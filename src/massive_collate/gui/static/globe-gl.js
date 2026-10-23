'use strict';
/* GPU renderer for the Markets globe (WebGL2). Draws everything that is per pixel or per vertex:
   the background stars and constellation lines (turning with sidereal time), the sphere with its
   night side and civil time-zone shading (the zone grid is a texture, offsets a second texture),
   the graticule, coastlines and borders (rotated in the vertex shader), and the Moon's surface.
   Markers, labels and the legend stay on the 2D overlay canvas above it (globe.js).

   Coordinates: Earth-fixed unit vectors (x towards 0°E on the equator, z north). The camera is
   given as three unit vectors: C towards the viewer, E to screen-right, N to screen-up. */
(() => {
  const VS_FULL = `#version 300 es
  void main() {
    vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
  }`;

  // The sphere: base colour, night side, time-zone shading, limb.
  const FS_SPHERE = `#version 300 es
  precision highp float; precision highp int; precision highp usampler2D;
  uniform vec2 uCenter; uniform float uR; uniform float uDpr; uniform float uHDev;
  uniform vec3 uE, uN, uC, uSun;
  uniform bool uDark; uniform bool uZones; uniform float uUtcMin; uniform float uSunLon;
  uniform usampler2D uZoneTex; uniform sampler2D uOffTex; uniform vec2 uZoneSize; uniform float uZoneRes;
  out vec4 frag;
  const float PI = 3.14159265358979;
  float bandAlpha(float k) {
    if (abs(k) > 8.0) return 0.0;
    if (k == 0.0) return 0.11;
    return 0.07 * (1.0 - (abs(k) - 1.0) / 8.0) * (mod(abs(k), 2.0) == 1.0 ? 1.0 : 0.55);
  }
  void main() {
    vec2 px = vec2(gl_FragCoord.x, uHDev - gl_FragCoord.y) / uDpr;
    float x = (px.x - uCenter.x) / uR, y = (uCenter.y - px.y) / uR;
    float rr = x * x + y * y;
    float edge = 1.0 / uR;  // one CSS pixel in sphere units
    if (rr > (1.0 + edge) * (1.0 + edge)) discard;
    float rho = sqrt(rr);
    float z = sqrt(max(0.0, 1.0 - rr));
    vec3 n = normalize(x * uE + y * uN + z * uC);
    float lat = asin(clamp(n.z, -1.0, 1.0)) * 180.0 / PI;
    float lon = atan(n.y, n.x) * 180.0 / PI;
    vec3 col = uDark ? vec3(0.043, 0.059, 0.094) : vec3(0.957, 0.965, 0.984);
    float hl = clamp(1.0 - distance(vec2(x, y), vec2(-0.3, 0.35)) / 1.35, 0.0, 1.0);
    col = mix(col, vec3(0.16, 0.38, 1.0), (uDark ? 0.03 : 0.02) + hl * (uDark ? 0.15 : 0.08));
    // night side, soft twilight edge
    float d = dot(n, uSun);
    if (d < 0.0) col = mix(col, vec3(0.024, 0.039, 0.094), min(1.0, -d * 4.0) * 0.55);
    // time zones: hours between local clock time and noon, fading away from it
    float k = 99.0;
    if (uZones) {
      ivec2 tc = ivec2(clamp(floor((lon + 180.0) / uZoneRes), 0.0, uZoneSize.x - 1.0),
                       clamp(floor((90.0 - lat) / uZoneRes), 0.0, uZoneSize.y - 1.0));
      uint zi = texelFetch(uZoneTex, tc, 0).r;
      if (zi > 0u) {
        float off = texelFetch(uOffTex, ivec2(int(zi), 0), 0).r;
        if (off > -9000.0) k = floor(mod(uUtcMin + off + 2880.0, 1440.0) / 60.0) - 12.0;
      }
    } else {
      k = floor((mod(lon - uSunLon + 540.0, 360.0) - 180.0) / 15.0 + 0.5);
    }
    col = mix(col, vec3(0.941, 0.725, 0.043), bandAlpha(k));
    // limb line and antialiased edge
    float limb = smoothstep(1.0 - 2.0 * edge, 1.0 - 0.5 * edge, rho);
    col = mix(col, uDark ? vec3(0.63, 0.67, 0.78) : vec3(0.16, 0.2, 0.31), limb * 0.55);
    float a = 1.0 - smoothstep(1.0 - 0.5 * edge, 1.0 + edge, rho);
    frag = vec4(col * a, a);
  }`;

  // Lines on the sphere (graticule, coastlines, borders): hidden on the far side.
  const VS_LINES = `#version 300 es
  in vec3 aP;
  uniform vec2 uCenter; uniform float uR; uniform vec2 uSize; uniform vec3 uE, uN, uC;
  out float vZ;
  void main() {
    float x = dot(aP, uE), y = dot(aP, uN);
    vZ = dot(aP, uC);
    vec2 px = vec2(uCenter.x + uR * x, uCenter.y - uR * y);
    gl_Position = vec4(px.x / uSize.x * 2.0 - 1.0, 1.0 - px.y / uSize.y * 2.0, 0.0, 1.0);
  }`;
  const FS_LINES = `#version 300 es
  precision mediump float;
  uniform vec4 uColor; in float vZ; out vec4 frag;
  void main() { if (vZ < 0.0) discard; frag = vec4(uColor.rgb * uColor.a, uColor.a); }`;

  // The sky: equatorial unit vectors turned by sidereal time, seen past the globe (gnomonic).
  const VS_SKY = `#version 300 es
  in vec3 aEq; in float aMag;
  uniform vec2 uCenter; uniform float uF; uniform vec2 uSize; uniform vec3 uE, uN, uC;
  uniform float uGmst; uniform float uDpr;
  out float vValid; out float vMag;
  void main() {
    float c = cos(uGmst), s = sin(uGmst);
    vec3 p = vec3(aEq.x * c + aEq.y * s, -aEq.x * s + aEq.y * c, aEq.z);
    float depth = -dot(p, uC);
    vValid = depth > 0.08 ? 1.0 : 0.0;
    vMag = aMag;
    float dd = max(depth, 0.08);
    vec2 px = vec2(uCenter.x + uF * dot(p, uE) / dd, uCenter.y - uF * dot(p, uN) / dd);
    gl_Position = vec4(px.x / uSize.x * 2.0 - 1.0, 1.0 - px.y / uSize.y * 2.0, 0.0, 1.0);
    gl_PointSize = max(1.0, 2.2 - aMag * 0.32) * uDpr;
  }`;
  const FS_STARS = `#version 300 es
  precision mediump float;
  uniform bool uDark; in float vValid; in float vMag; out vec4 frag;
  void main() {
    if (vValid < 0.5) discard;
    float r = length(gl_PointCoord - 0.5);
    if (r > 0.5) discard;
    float a = uDark ? max(0.15, 0.9 - vMag * 0.12) : max(0.12, 0.7 - vMag * 0.1);
    vec3 c = uDark ? vec3(0.9, 0.92, 1.0) : vec3(0.16, 0.2, 0.35);
    a *= 1.0 - smoothstep(0.35, 0.5, r);
    frag = vec4(c * a, a);
  }`;
  const FS_SKYLINES = `#version 300 es
  precision mediump float;
  uniform vec4 uColor; in float vValid; in float vMag; out vec4 frag;
  void main() { if (vValid < 0.999) discard; frag = vec4(uColor.rgb * uColor.a, uColor.a); }`;

  // The Moon: its near side (maria, bright craters, fine texture) lit by the Sun, dim.
  const FS_MOON = `#version 300 es
  precision highp float;
  uniform vec2 uMoon; uniform float uMoonR; uniform float uDpr; uniform float uHDev;
  uniform vec3 uE, uN, uC, uSun, uF, uU, uEast; uniform bool uDark;
  uniform vec4 uMaria[16]; uniform vec4 uBright[3];
  out vec4 frag;
  const float D = 0.017453292519943295;
  float hash(vec3 p) { return fract(sin(dot(p, vec3(127.1, 311.7, 74.7))) * 43758.5453); }
  float noise(vec3 v, float s) {
    vec3 p = v * s, i = floor(p), f = p - i;
    vec3 u = f * f * (3.0 - 2.0 * f);
    float n = 0.0;
    for (int dx = 0; dx <= 1; dx++) for (int dy = 0; dy <= 1; dy++) for (int dz = 0; dz <= 1; dz++) {
      vec3 o = vec3(dx, dy, dz);
      vec3 w = mix(1.0 - u, u, o);
      n += w.x * w.y * w.z * hash(i + o);
    }
    return n;
  }
  float arcDeg(float la1, float lo1, float la2, float lo2) {
    return acos(clamp(sin(la1 * D) * sin(la2 * D) + cos(la1 * D) * cos(la2 * D) * cos((lo1 - lo2) * D), -1.0, 1.0)) / D;
  }
  void main() {
    vec2 px = vec2(gl_FragCoord.x, uHDev - gl_FragCoord.y) / uDpr;
    float x = (px.x - uMoon.x) / uMoonR, y = (uMoon.y - px.y) / uMoonR;
    float rr = x * x + y * y;
    if (rr > 1.0) discard;
    float z = sqrt(1.0 - rr);
    vec3 n = normalize(x * uE + y * uN + z * uC);
    float lat = asin(clamp(dot(n, uU), -1.0, 1.0)) / D;
    float lon = atan(dot(n, uEast), dot(n, uF)) / D;
    float a = 0.78 + 0.10 * (noise(n, 9.0) - 0.5) + 0.06 * (noise(n, 23.0) - 0.5);
    for (int i = 0; i < 16; i++) {
      vec4 m = uMaria[i];
      float dist = arcDeg(lat, lon, m.x, m.y);
      float e = m.z * (0.8 + 0.4 * noise(n, 5.0));
      if (dist < e * 1.25) a -= m.w * 0.8 * clamp((e * 1.25 - dist) / (e * 0.7), 0.0, 1.0);
    }
    for (int i = 0; i < 3; i++) {
      vec4 b = uBright[i];
      float dist = arcDeg(lat, lon, b.x, b.y);
      if (dist < b.z * 6.0) a += b.w * 0.6 * exp(-dist / b.z) * (dist < b.z ? 1.0 : 0.35);
    }
    a = clamp(a, 0.15, 1.0);
    float s = dot(n, uSun);
    float light = s > 0.0 ? min(1.0, 0.25 + s * 0.9) : max(0.0, 0.04 + s * 0.2);
    float v = 0.47 * a * light;
    float edge = clamp((1.0 - sqrt(rr)) * uMoonR * 0.8, 0.0, 1.0);
    float alpha = edge * (uDark ? 0.42 : 0.32);
    frag = vec4(vec3(v, v * 0.98, v * 0.94) * alpha, alpha);
  }`;

  function compile(gl, vs, fs) {
    const prog = gl.createProgram();
    for (const [type, src] of [[gl.VERTEX_SHADER, vs], [gl.FRAGMENT_SHADER, fs]]) {
      const sh = gl.createShader(type);
      gl.shaderSource(sh, src);
      gl.compileShader(sh);
      if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(sh));
      gl.attachShader(prog, sh);
    }
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog));
    const u = {};
    const n = gl.getProgramParameter(prog, gl.ACTIVE_UNIFORMS);
    for (let i = 0; i < n; i++) {
      const name = gl.getActiveUniform(prog, i).name.replace(/\[0\]$/, '');
      u[name] = gl.getUniformLocation(prog, name);
    }
    return { prog, u };
  }

  const rad = Math.PI / 180;
  const toUnit = (lon, lat) => [Math.cos(lat * rad) * Math.cos(lon * rad), Math.cos(lat * rad) * Math.sin(lon * rad), Math.sin(lat * rad)];

  // Polylines of [lon, lat] -> GL_LINES vertex buffer of unit vectors.
  function segments(polylines, interpolate = 0) {
    let count = 0;
    for (const line of polylines) count += Math.max(0, line.length - 1) * (interpolate || 1);
    const out = new Float32Array(count * 6);
    let k = 0;
    for (const line of polylines) {
      for (let i = 0; i + 1 < line.length; i++) {
        const a = toUnit(line[i][0], line[i][1]), b = toUnit(line[i + 1][0], line[i + 1][1]);
        out.set(a, k); out.set(b, k + 3); k += 6;
      }
    }
    return out.subarray(0, k);
  }

  class GlobeGL {
    constructor(canvas) {
      const gl = canvas.getContext('webgl2', { premultipliedAlpha: true, antialias: true, alpha: true });
      if (!gl) throw new Error('WebGL2 unavailable');
      this.canvas = canvas;
      this.gl = gl;
      this.sphere = compile(gl, VS_FULL, FS_SPHERE);
      this.lines = compile(gl, VS_LINES, FS_LINES);
      this.stars = compile(gl, VS_SKY, FS_STARS);
      this.skyLines = compile(gl, VS_SKY, FS_SKYLINES);
      this.moon = compile(gl, VS_FULL, FS_MOON);
      this.empty = gl.createVertexArray();
      this.buffers = {};
      this.zones = null;
      this.offTex = null;
      const grat = [];
      for (let lon = -180; lon < 180; lon += 15) grat.push(Array.from({ length: 91 }, (_, i) => [lon, -90 + i * 2]));
      for (let lat = -75; lat <= 75; lat += 15) if (lat) grat.push(Array.from({ length: 181 }, (_, i) => [-180 + i * 2, lat]));
      this.setLines('graticule', grat);
      this.setLines('equator', [Array.from({ length: 181 }, (_, i) => [-180 + i * 2, 0])]);
    }

    _vao(prog, attrs, data) {
      const gl = this.gl;
      const vao = gl.createVertexArray();
      gl.bindVertexArray(vao);
      const buf = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, buf);
      gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
      const stride = attrs.reduce((s, a) => s + a[1], 0) * 4;
      let off = 0;
      for (const [name, size] of attrs) {
        const loc = gl.getAttribLocation(prog, name);
        if (loc >= 0) {
          gl.enableVertexAttribArray(loc);
          gl.vertexAttribPointer(loc, size, gl.FLOAT, false, stride, off);
        }
        off += size * 4;
      }
      gl.bindVertexArray(null);
      return vao;
    }

    setLines(name, polylines) {
      const data = segments(polylines);
      this.buffers[name] = { vao: this._vao(this.lines.prog, [['aP', 3]], data), count: data.length / 3 };
    }

    deleteLines(name) {
      const b = this.buffers[name];
      if (!b) return;
      this.gl.deleteVertexArray(b.vao);
      delete this.buffers[name];
    }

    setSky(stars, lines) {
      const s = new Float32Array(stars.length * 4);
      stars.forEach(([ra, dec, mag], i) => { s.set([...toUnit(ra, dec), mag], i * 4); });
      this.buffers.stars = { vao: this._vao(this.stars.prog, [['aEq', 3], ['aMag', 1]], s), count: stars.length };
      const segs = segments(lines);
      const withMag = new Float32Array((segs.length / 3) * 4);
      for (let i = 0; i < segs.length / 3; i++) withMag.set([segs[i * 3], segs[i * 3 + 1], segs[i * 3 + 2], 0], i * 4);
      this.buffers.skyLines = { vao: this._vao(this.skyLines.prog, [['aEq', 3], ['aMag', 1]], withMag), count: segs.length / 3 };
    }

    setZones(width, height, grid, res) {
      const gl = this.gl;
      const tex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, tex);
      gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.R16UI, width, height, 0, gl.RED_INTEGER, gl.UNSIGNED_SHORT, grid);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
      this.zones = { tex, width, height, res };
    }

    // offsets: minutes per zone index (index 0 unused); null -> no shading there
    setZoneOffsets(offsets) {
      const gl = this.gl;
      const data = new Float32Array(Math.max(1, offsets.length));
      offsets.forEach((v, i) => { data[i] = v == null ? -99999 : v; });
      if (!this.offTex) this.offTex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, this.offTex);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.R32F, data.length, 1, 0, gl.RED, gl.FLOAT, data);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    }

    _camera(p, u) {
      const gl = this.gl;
      if (u.uE) gl.uniform3fv(u.uE, p.E);
      if (u.uN) gl.uniform3fv(u.uN, p.N);
      if (u.uC) gl.uniform3fv(u.uC, p.C);
      if (u.uCenter) gl.uniform2f(u.uCenter, p.cx, p.cy);
      if (u.uR) gl.uniform1f(u.uR, p.r);
      if (u.uSize) gl.uniform2f(u.uSize, p.w, p.h);
      if (u.uDpr) gl.uniform1f(u.uDpr, p.dpr);
      if (u.uHDev) gl.uniform1f(u.uHDev, this.canvas.height);
      if (u.uDark) gl.uniform1i(u.uDark, p.dark ? 1 : 0);
    }

    _moon(p) {
      const gl = this.gl, m = this.moon, u = m.u, mo = p.moon;
      gl.useProgram(m.prog);
      this._camera(p, u);
      gl.uniform2f(u.uMoon, mo.x, mo.y);
      gl.uniform1f(u.uMoonR, mo.r);
      gl.uniform3fv(u.uSun, p.S);
      gl.uniform3fv(u.uF, mo.f); gl.uniform3fv(u.uU, mo.u); gl.uniform3fv(u.uEast, mo.east);
      gl.uniform4fv(u.uMaria, mo.maria);
      gl.uniform4fv(u.uBright, mo.bright);
      gl.bindVertexArray(this.empty);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    }

    render(p) {
      const gl = this.gl, c = this.canvas;
      const W = Math.round(p.w * p.dpr), H = Math.round(p.h * p.dpr);
      if (c.width !== W || c.height !== H) { c.width = W; c.height = H; }
      gl.viewport(0, 0, W, H);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);

      // sky
      if (this.buffers.stars) {
        for (const [prog, buf, mode] of [[this.skyLines, this.buffers.skyLines, gl.LINES], [this.stars, this.buffers.stars, gl.POINTS]]) {
          gl.useProgram(prog.prog);
          this._camera(p, prog.u);
          gl.uniform1f(prog.u.uF, p.r * 1.15);
          gl.uniform1f(prog.u.uGmst, p.gmst * rad);
          if (prog.u.uColor) gl.uniform4fv(prog.u.uColor, p.dark ? [0.47, 0.59, 0.86, 0.22] : [0.24, 0.31, 0.55, 0.18]);
          gl.bindVertexArray(buf.vao);
          gl.drawArrays(mode, 0, buf.count);
        }
      }
      if (p.moon && p.moon.behind) this._moon(p);

      // sphere
      const s = this.sphere, u = s.u;
      gl.useProgram(s.prog);
      this._camera(p, u);
      gl.uniform3fv(u.uSun, p.S);
      gl.uniform1f(u.uSunLon, p.sunLon);
      gl.uniform1f(u.uUtcMin, p.utcMin);
      const zones = !!(this.zones && this.offTex);
      gl.uniform1i(u.uZones, zones ? 1 : 0);
      if (zones) {
        gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, this.zones.tex); gl.uniform1i(u.uZoneTex, 0);
        gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, this.offTex); gl.uniform1i(u.uOffTex, 1);
        gl.uniform2f(u.uZoneSize, this.zones.width, this.zones.height);
        gl.uniform1f(u.uZoneRes, this.zones.res);
      }
      gl.bindVertexArray(this.empty);
      gl.drawArrays(gl.TRIANGLES, 0, 3);

      // lines on the sphere
      const L = this.lines;
      gl.useProgram(L.prog);
      this._camera(p, L.u);
      const tone = p.dark ? [0.63, 0.67, 0.78] : [0.16, 0.2, 0.31];
      const sets = [['graticule', 0.16], ['equator', 0.3]];
      if (p.tiles && p.tiles.length) for (const t of p.tiles) sets.push([t, 0.62]);
      else sets.push([p.outlines, 0.62]);
      for (const [name, alpha] of sets) {
        const b = this.buffers[name];
        if (!b) continue;
        gl.uniform4fv(L.u.uColor, [...tone, alpha]);
        gl.bindVertexArray(b.vao);
        gl.drawArrays(gl.LINES, 0, b.count);
      }
      if (p.moon && !p.moon.behind) this._moon(p);
      gl.bindVertexArray(null);
    }
  }

  window.GlobeGL = GlobeGL;
})();
