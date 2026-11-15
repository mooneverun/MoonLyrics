"""Neutral Glasswork transmission with Atrium's continuous, full end profile.

Glasswork, commit 4f0b6744b898b3a9b98fbd913ab24fdb83c53162: interior blur,
refraction falloff, dispersion, adaptive opacity and inset highlights.
Atrium, commit 2e51d0363c4b37a170b34338aa3776d5d328c450: continuous corner
profile, used consistently for coverage, refraction normals and highlights.
See assets/Glasswork-LICENSE.txt and assets/Atrium-LICENSE.txt.
"""
from functools import lru_cache


@lru_cache(maxsize=4)
def _sample_offsets(cw, ch, fw, fh, ox, oy, dpr, top_down):
    patches = []
    for fy in (.24, .5, .76):
        for fx in (.18, .34, .5, .66, .82):
            patch = []
            for dy in (-3, -1, 1, 3):
                for dx in (-3, -1, 1, 3):
                    x = max(0, min(cw-1, round(ox+fw*fx+dx*dpr)))
                    y = max(0, min(ch-1, round(oy+fh*fy+dy*dpr)))
                    if top_down:
                        y = ch-1-y
                    patch.append((y*cw+x)*4)
            patches.append(tuple(patch))
    return tuple(patches)


def sample_background(pixels, size, frame, offset, dpr, top_down=False):
    """Read the original 15 patches through a zero-copy byte view."""
    pixels = memoryview(pixels).cast('B')
    colors = []
    for patch in _sample_offsets(*size, *frame, *offset, dpr, top_down):
        red = green = blue = 0
        for index in patch:
            red += pixels[index+2]; green += pixels[index+1]; blue += pixels[index]
        colors.append((red/16, green/16, blue/16))
    return tuple(colors)


def _smoothstep(value):
    value = max(0., min(1., value))
    return value * value * (3 - 2 * value)


def _luminance(rgb):
    channels = [value / 12.92 if value <= .03928 else ((value + .055) / 1.055) ** 2.4
                for value in rgb]
    return sum(value * weight for value, weight in zip(channels, (.2126, .7152, .0722)))


def background_tone(colors, night):
    """Keep adaptive readability without changing the background's hue."""
    if not colors:
        return .16 + .22 * .35 if night else .06 + .16 * .35
    samples = sorted((sum(v * w for v, w in zip(rgb, (.299, .587, .114))) / 255, rgb)
                     for rgb in colors)
    texture = samples[-1][0] - samples[0][0]
    kept = samples[2:-2] if len(samples) > 4 else samples
    mean = sum(item[0] for item in kept) / len(kept)
    color = [sum(item[1][channel] for item in kept) / len(kept) for channel in range(3)]
    chroma = (max(color) - min(color)) / 255
    near = _smoothstep((mean - .12) / .4 if night else (.8 - mean) / .35)
    density = max(0., min(1., near * .55 + texture * .7 + chroma * .3))
    tint = (30, 30, 30) if night else (255, 255, 255)
    tint_level = sum(v * w for v, w in zip(tint, (.299, .587, .114))) / 255
    backdrop = min(1., mean + texture / 2) if night else max(0., mean - texture / 2)
    ink = _luminance(tuple(v / 255 for v in ((245, 245, 247) if night else (29, 29, 31))))

    def reads(alpha):
        gray = round(max(0., min(1., backdrop + (tint_level - backdrop) * alpha)) * 255) / 255
        background = _luminance((gray, gray, gray))
        return (max(ink, background) + .05) / (min(ink, background) + .05) >= 4.5

    floor = 0.
    if not reads(0.):
        low, high = 0., .9
        if reads(high):
            for _ in range(16):
                middle = (low + high) / 2
                if reads(middle):
                    high = middle
                else:
                    low = middle
        floor = high
    thin, dense = (.16, .38) if night else (.06, .22)
    return min(.95, max(floor, thin + (dense - thin) * density))


CORNER_PROFILE = """
uniform float uCornerRadius;
vec2 cornerProfile(vec2 b,float r) {
    r=clamp(r,0.,min(b.x,b.y));
    float reach=min(r*1.6,min(b.x,b.y));
    float v=1.-.2928932188134525*r/max(reach,1e-12);
    float v2=v*v;
    float a=b.x==b.y && r==b.x ? 0. : max(.02,(2.*v2*v2-v2)/max(1.-v2,1e-12));
    return vec2(reach,a);
}
float cornerEdge(vec2 q,vec2 profile) {
    float r=profile.x,a=profile.y;
    if(r<=0. || min(q.x,q.y)<=0.) return max(q.x,q.y)-r;
    vec2 v=q/r,v2=v*v;
    vec2 den=a+(1.-a)*v2;
    vec2 derivative=2.*v*v2*(2.*a+(1.-a)*v2)/(den*den);
    return (dot(v2*v2/den,vec2(1.))-1.)*r/max(length(derivative),1e-12);
}
float sdCorner(vec2 q,float r,float a) {
    if(min(q.x,q.y)<=0.) return max(q.x,q.y)-r;
    if(r<=0.) return length(q);
    float n=length(q);
    vec2 v,derivative;
    for(int i=0;i<2;++i) {
        v=q/n;
        vec2 v2=v*v;
        vec2 den=a+(1.-a)*v2;
        vec2 f=v2*v2/den;
        derivative=2.*v*v2*(2.*a+(1.-a)*v2)/(den*den);
        n*=1.+(f.x+f.y-1.)/dot(v,derivative);
    }
    return (n-r)*dot(v,derivative)/length(derivative);
}
float sdRoundRect(vec2 p,vec2 b,float r,bool edge) {
    vec2 profile=cornerProfile(b,r);
    vec2 q=abs(p)-b+profile.x;
    return edge ? cornerEdge(q,profile) : sdCorner(q,profile.x,profile.y);
}
// Radius 30 at the approved 80 px height. Preserve the same full-end
// proportions at other heights, rather than growing a straight end segment.
float profileRadius(vec2 halfSize) {
    return uCornerRadius>0. ? min(uCornerRadius,min(halfSize.x,halfSize.y))
                           : min(halfSize.x,halfSize.y)*.75;
}
"""


LENS = """#version 330 core
in vec2 uv; out vec4 outputColor;
uniform vec2 uSize;
uniform float uRim;
""" + CORNER_PROFILE + """
float distanceAt(vec2 p) {
    return sdRoundRect(p,uSize*.5,profileRadius(uSize*.5),false);
}
void main() {
    vec2 p=(vec2(uv.x,1.-uv.y)-.5)*uSize;
    float depth=max(-distanceAt(p),0.);
    vec2 bend=vec2(0.);
    if(depth<uRim) {
        const float e=.5;
        vec2 g=vec2(distanceAt(p+vec2(e,0.))-distanceAt(p-vec2(e,0.)),
                    distanceAt(p+vec2(0.,e))-distanceAt(p-vec2(0.,e)));
        float strength=pow(max(0.,1.-depth/uRim),2.6);
        bend=-g/max(length(g),1e-8)*strength;
    }
    outputColor=vec4(floor(vec2(128.)+bend*127.+.5)/255.,.5,1.);
}
"""


GLASS = """#version 330 core
in vec2 uv; out vec4 outputColor;
uniform sampler2D uSource,uLens;
uniform vec2 uFrameSize,uCaptureSize,uOrigin;
uniform vec4 uBounds;
uniform float uDpr,uNight,uTintAlpha,uRim;
""" + CORNER_PROFILE + """
vec3 transmitted(vec2 shift) {
    vec2 point=(uv*uFrameSize+uOrigin+vec2(shift.x,-shift.y)*uDpr)/uCaptureSize;
    vec3 c=texture(uSource,clamp(point,uBounds.xy,uBounds.zw)).rgb;
    return c*mix(1.,.8,uNight);
}
float normalCdf(float x) {
    float t=1./(1.+.2316419*abs(x));
    float tail=.39894228*exp(-.5*x*x)*t*
      (.31938153+t*(-.35656378+t*(1.78147794+t*(-1.82125598+t*1.33027443))));
    return x>=0.?1.-tail:tail;
}
float insetShadow(vec2 p,vec2 halfSize,float radius,vec2 shift,float blur,float spread) {
    float distance=sdRoundRect(p-shift,halfSize-vec2(spread),max(0.,radius-spread),false);
    return blur<.001?step(0.,distance):normalCdf(distance/max(.001,blur*.5));
}
vec3 shadow(vec3 c,vec3 ink,float alpha) { return mix(c,ink,clamp(alpha,0.,1.)); }
void main() {
    vec2 halfSize=uFrameSize/uDpr*.5;
    vec2 p=(vec2(uv.x,1.-uv.y)-.5)*uFrameSize/uDpr;
    float radius=profileRadius(halfSize);
    float sdf=sdRoundRect(p,halfSize,radius,false);
    vec2 map=texture(uLens,uv).rg-.5;
    float scale=2.*1.1*uRim;
    vec3 scales=floor(scale*vec3(1.15,1.,.85)*10.+.5)/10.;
    vec2 offset=map*scales.g;
    vec3 color=vec3(transmitted(map*scales.r).r,transmitted(offset).g,transmitted(map*scales.b).b);
    vec3 tint=mix(vec3(1.),vec3(30.)/255.,uNight);
    color=mix(color,tint,uTintAlpha);
    // The inset box-shadow stack from glass.css, painted bottom to top.
    color=shadow(color,vec3(0.),insetShadow(p,halfSize,radius,vec2(0.,-6.),8.,-6.)*mix(.06,.08,uNight));
    color=shadow(color,vec3(1.),insetShadow(p,halfSize,radius,vec2(-4.,0.),8.,-6.)*mix(.12,.04,uNight));
    color=shadow(color,vec3(1.),insetShadow(p,halfSize,radius,vec2(4.,0.),8.,-6.)*mix(.22,.07,uNight));
    color=shadow(color,vec3(1.),insetShadow(p,halfSize,radius,vec2(0.,6.),8.,-6.)*mix(.50,.18,uNight));
    color=shadow(color,vec3(1.),insetShadow(p,halfSize,radius,vec2(0.),0.,.5)*mix(.30,.06,uNight));
    float shift=mix(1.5,1.,uNight),blur=mix(1.,.5,uNight),spread=mix(-1.,-.5,uNight);
    color=shadow(color,vec3(1.),insetShadow(p,halfSize,radius,vec2(-shift),blur,spread)*mix(.60,.18,uNight));
    color=shadow(color,vec3(1.),insetShadow(p,halfSize,radius,vec2(shift),blur,spread)*mix(1.,.46,uNight));
    float edgeDistance=sdRoundRect(p,halfSize,radius,true);
    float aa=max(fwidth(edgeDistance),1e-3);
    float alpha=sdf < -2. ? 1. : clamp(.5-edgeDistance/aa,0.,1.);
    outputColor=vec4(color*alpha,alpha);
}
"""
