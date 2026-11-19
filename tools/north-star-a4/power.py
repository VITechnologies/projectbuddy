import numpy as np, itertools, sys
rng=np.random.default_rng(379)
N=18
signs=np.array(list(itertools.product([1,-1],repeat=N)),dtype=np.int8)  # 262144 x 18
def lg(p): return np.log(p/(1-p))
def sg(x): return 1/(1+np.exp(-x))
def pperm(d):
    # one-sided exact sign-flip p for H0 mean<=0 vs mean>0
    obs=d.sum(); s=signs@d
    return (s>=obs-1e-12).mean()
def sim(p0,p1,r0,r1,delta,sims=1500,su=0.8,se=0.5,mc=5.0,mref=5.0,alpha=0.025,paired=True):
    a1=a2=both=0
    for _ in range(sims):
        u=rng.normal(0,su,N); v=rng.normal(0,su,N)
        ncC=rng.poisson(mc,N)+2; ncT=rng.poisson(mc,N)+2; nr=rng.poisson(mref-2,N)+2
        uu=u
        def draw(mu,n,ee): 
            p=sg(mu+uu+rng.normal(0,se,N)); return rng.binomial(n,p)
        # unsupported
        xC=draw(lg(p0),ncC,0); xT=draw(lg(p1),ncT,0)
        dU=xC/ncC-xT/ncT   # positive = treatment better
        # retention
        rC=draw(lg(r0),nr,0); rT=draw(lg(r1),nr,0)
        dR=(rT-rC)/nr
        p_u=pperm(dU)
        # NI: H0 mean(dR)<=-delta ; shift
        p_r=pperm(dR+delta)
        a=p_u<alpha; b=p_r<alpha
        a1+=a;a2+=b;both+=a&b
    return a1/sims,a2/sims,both/sims
if __name__=="__main__":
    for (r0,r1) in [(0.85,0.85),(0.85,0.80),(0.85,0.75)]:
        for delta in [0.10,0.15,0.20]:
            print(r0,r1,delta,sim(0.26,0.04,r0,r1,delta,sims=600))
