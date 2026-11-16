using System.Diagnostics;
using System.Diagnostics.CodeAnalysis;
using System.Net;
using System.Net.NetworkInformation;
using System.Text;
using ProjectBuddy.PeerOllama;

namespace ProjectBuddy.PeerProbes;

// The operating system behind ISources. Each method is a thin read with no decisions of its own; the registry and the
// parsers that interpret its output are tested against fakes on Linux.
[ExcludeFromCodeCoverage(Justification = "Operating-system adapters; the registry and parsers are tested against fakes.")]
public sealed class SystemSources(Uri ollama, string? vboxManage) : ISources
{
    public const int MaxOutputChars = 16 * 1024 * 1024;

    public static readonly TimeSpan CommandTimeout = TimeSpan.FromSeconds(60);

    public bool IsWindows => OperatingSystem.IsWindows();

    public IReadOnlyList<Adapter> Adapters()
    {
        var adapters = new List<Adapter>();
        foreach (var nic in NetworkInterface.GetAllNetworkInterfaces())
        {
            var kind = nic.NetworkInterfaceType switch
            {
                NetworkInterfaceType.Ethernet or NetworkInterfaceType.Ethernet3Megabit or NetworkInterfaceType.FastEthernetT
                    or NetworkInterfaceType.FastEthernetFx or NetworkInterfaceType.GigabitEthernet => "ethernet",
                NetworkInterfaceType.Wireless80211 => "wifi",
                NetworkInterfaceType.Loopback => "loopback",
                NetworkInterfaceType.Tunnel => "tunnel",
                _ => "other",
            };
            long speed;
            try
            {
                speed = nic.Speed > 0 ? nic.Speed / 1_000_000 : 0;
            }
            catch (PlatformNotSupportedException)
            {
                speed = 0;
            }

            IReadOnlyList<string> gateways;
            try
            {
                gateways = [.. nic.GetIPProperties().GatewayAddresses.Select(gateway => gateway.Address)
                    .Where(address => !address.Equals(IPAddress.Any) && !address.Equals(IPAddress.IPv6Any)).Select(address => address.ToString())];
            }
            catch (Exception error) when (error is PlatformNotSupportedException or NetworkInformationException)
            {
                gateways = [];
            }

            adapters.Add(new Adapter(nic.Name, kind, nic.OperationalStatus.ToString().ToLowerInvariant(), speed,
                nic.Supports(NetworkInterfaceComponent.IPv4), nic.Supports(NetworkInterfaceComponent.IPv6), gateways));
        }

        return adapters;
    }

    public IReadOnlyList<Drive> Drives()
    {
        var drives = new List<Drive>();
        foreach (var drive in DriveInfo.GetDrives().Where(drive => drive.DriveType == DriveType.Fixed))
        {
            try
            {
                if (drive.IsReady)
                {
                    drives.Add(new Drive(drive.Name, drive.DriveFormat, drive.TotalSize, drive.AvailableFreeSpace));
                }
            }
            catch (Exception error) when (error is IOException or UnauthorizedAccessException)
            {
                // A drive that cannot be read is left out.
            }
        }

        return drives;
    }

    public async Task<OllamaState?> OllamaAsync()
    {
        try
        {
            using var client = new OllamaClient(OllamaClient.LoopbackHandler(), ollama, TimeSpan.FromSeconds(10));
            var version = await client.VersionAsync();
            var models = (await client.DigestsAsync()).Select(model => new OllamaModel(model.Key, model.Value)).ToList();
            return new OllamaState(version, models, await client.LoadedAsync());
        }
        catch (PeerException)
        {
            return null;
        }
    }

    public CommandResult? Run(string program, IReadOnlyList<string> arguments)
    {
        if (Resolve(program) is not { } path)
        {
            return null;
        }

        var start = new ProcessStartInfo(path)
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8,
        };
        foreach (var argument in arguments)
        {
            start.ArgumentList.Add(argument);
        }

        using var process = Process.Start(start)!;
        _ = process.StandardError.BaseStream.CopyToAsync(Stream.Null);
        var output = new StringBuilder();
        var truncated = false;

        // Output past the cap is read and dropped, so the program finishes and its own exit code is reported.
        var reader = Task.Run(() =>
        {
            var buffer = new char[8192];
            int read;
            while ((read = process.StandardOutput.Read(buffer, 0, buffer.Length)) > 0)
            {
                var keep = Math.Min(read, MaxOutputChars - output.Length);
                output.Append(buffer, 0, keep);
                truncated |= keep < read;
            }
        });
        if (!process.WaitForExit(CommandTimeout))
        {
            process.Kill(entireProcessTree: true);
            process.WaitForExit();
            reader.Wait(TimeSpan.FromSeconds(5));
            return new CommandResult(-1, output.ToString(), true);
        }

        // A child that inherited the pipe can hold it open after the program exits; what was read by then is kept.
        var drained = reader.Wait(TimeSpan.FromSeconds(5));
        return new CommandResult(process.ExitCode, output.ToString(), truncated || !drained);
    }

    private string? Resolve(string program) =>
        ProgramPaths.Candidates(program, vboxManage, OperatingSystem.IsWindows(), Environment.SystemDirectory,
            Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles)).FirstOrDefault(File.Exists);
}
