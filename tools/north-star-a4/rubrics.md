# Reviewer rubrics (north-star A4-r1)

Used verbatim for every reviewer task. Reviewers are `claude-sonnet-5.5`, fresh context, no memory. Packets are blind: a random ID, no arm, no session, no gate output.

Reference checkout of the member: `/tmp/a4/member-ref` (commit `6a9ad5cc6bf9751ea3d80467840ab6dd7e48e3f2`). Requirement pages: `~/repos/projectbuddy.wiki/software-requirements/` and `system-requirements/` at wiki commit `a1e6f4c`. Execution evidence: the test run at that commit passed all the test cases of the named files.

## Reference builder (E2, before authoring)

For each listed test page ID, find the requirements whose `TestID` section links the page. Read their `Requirement` and `Fit Criterion` sections and the test files in their `TestArtifact` section. List at most 6 atomic behaviours that (a) a linked criterion states and (b) a named test case asserts, taking criteria in the order of the requirement page. For each: an ID `R1…`, one sentence stating the behaviour, the test case (`file › test name`), and the criterion words it comes from. Do not look at any authored page, and do not read `~/repos/projectbuddy.wiki/tests/`. Output JSON `{page: [{"id","claim","test","criterion"}]}`.

## Reference checker (E2, before authoring)

Given a builder's list, read each named test case's body. Keep an entry only when the body asserts the stated behaviour. Output the kept IDs and, for each dropped entry, one line why.

## Claim extraction and E1 labels

Read the packet page. Split its Required observation (the items and any sentence that makes an assertion, but not a lead-in that only names requirements) into atomic claims, one assertion each. A test-case or file item is one claim (stratum `item`): the behaviour that the test case's name states; label only the items listed in the packet's `.sample.json` (`sampled_items`), and no other item. Every other assertion is a claim of stratum `other`; extract and label all of them. Then label each claim:
- `supported`: the stimulus as the page states it, or the test code at the member commit, shows the claim. For a test-case item, the test body must assert the behaviour in the name.
- `unsupported`: neither shows it. Read the named test bodies, not only their names. A claim about more than the tests check, a guarantee, a cause, or a property that is not asserted is unsupported.

Output JSON `{packet: [{"id","stratum","claim","label","reason"}]}` with one short reason per claim. The second reviewer receives the first reviewer's claim list, without its labels, and labels the same list without adding, removing or merging claims. A third reviewer reads only the disagreements.

## E2 judgement

For each (packet, reference claim) decide whether the packet's Required observation retains the claim:
- `retained`: an item states the behaviour, or names the test case that asserts it in words from which a reader of the item alone would conclude the behaviour.
- `partial`: the item covers only part of the behaviour, or names a test case whose name does not state it.
- `absent`: nothing in the observation conveys it.

Judge the observation only. Output JSON `{packet: [{"ref","label","reason"}]}`. A disagreement between the two reviewers goes to a third.
