'use strict';
/*
 * GPU bar renderer (WebGL2 = OpenGL ES 3.0; runs on NVIDIA, AMD and Intel through the
 * browser's GL layer: Direct3D on Windows, Metal on macOS, OpenGL/Vulkan on Linux).
 *
 * Bars are uploaded once (index, open, high, low, close, volume per bar, prices relative
 * to a reference to keep float32 precision). Each frame passes only the x/y mapping as
 * uniforms; the vertex shader places every wick, body and tick, so the CPU does no
 * per-bar work while zooming or scrolling.
 */
(function () {
  const VS = `#version 300 es
    layout(location = 0) in vec2 corner;      // unit quad
    layout(location = 1) in float idx;        // bar index
    layout(location = 2) in vec4 ohlc;        // open, high, low, close (minus refPrice)
    layout(location = 3) in float vol;
    uniform vec2 res;          // canvas size, device px
    uniform float dpr;
    uniform vec2 xMap;         // x(css px) = floor(xMap.x + idx * xMap.y + 0.5), as the 2D chart does
    uniform vec2 yMap;         // y(css px) = yMap.x + price * yMap.y   (price relative to refPrice)
    uniform int part;          // 0 wick, 1 body, 2 open tick, 3 close tick
    uniform float bodyWidth;   // css px
    uniform float maxVol;      // > 0: body width scales with volume (volume candles)
    out float up;
    void main() {
      float o = ohlc.x, h = ohlc.y, l = ohlc.z, c = ohlc.w;
      up = c >= o ? 1.0 : 0.0;
      float x = floor(xMap.x + idx * xMap.y + 0.5);
      float w = bodyWidth;
      if (maxVol > 0.0) w = max(1.0, bodyWidth * vol / maxVol);
      float y0, y1, cx = x, cw = w;
      if (part == 0) { y0 = h; y1 = l; cw = 1.0; }
      else if (part == 1) { y0 = max(o, c); y1 = min(o, c); }
      else if (part == 2) { y0 = o; y1 = o; cx = x - w * 0.25; cw = w * 0.5; }
      else { y0 = c; y1 = c; cx = x + w * 0.25; cw = w * 0.5; }
      // Same whole-pixel rounding as the chart's y axis (Math.round).
      float py0 = floor(yMap.x + y0 * yMap.y + 0.5), py1 = floor(yMap.x + y1 * yMap.y + 0.5);
      if (abs(py1 - py0) < 1.0) py1 = py0 + 1.0;          // at least one pixel tall
      float px = (cx + (corner.x - 0.5) * cw) * dpr;
      float py = mix(py0, py1, corner.y) * dpr;
      gl_Position = vec4(px / res.x * 2.0 - 1.0, 1.0 - py / res.y * 2.0, 0.0, 1.0);
    }`;
  const FS = `#version 300 es
    precision mediump float;
    in float up;
    uniform vec4 upColor, downColor;
    out vec4 outColor;
    void main() { outColor = up > 0.5 ? upColor : downColor; }`;

  function compile(gl, type, src) {
    const s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
    return s;
  }

  const colorCache = new Map();
  function parseColor(css) {
    if (!colorCache.has(css)) colorCache.set(css, parseColorUncached(css));
    return colorCache.get(css);
  }

  function parseColorUncached(css) {
    const c = document.createElement('canvas').getContext('2d');
    c.fillStyle = css;
    const v = c.fillStyle;  // normalised: #rrggbb or rgba(...)
    if (v.startsWith('#')) return [1, 3, 5].map((i) => parseInt(v.slice(i, i + 2), 16) / 255).concat(1);
    const m = v.match(/[\d.]+/g).map(Number);
    return [m[0] / 255, m[1] / 255, m[2] / 255, m.length > 3 ? m[3] : 1];
  }

  class GpuBars {
    static probe() {
      try {
        const gl = document.createElement('canvas').getContext('webgl2');
        if (!gl) return { supported: false, renderer: null };
        const dbg = gl.getExtension('WEBGL_debug_renderer_info');
        const renderer = dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
        gl.getExtension('WEBGL_lose_context')?.loseContext();
        return { supported: true, renderer, software: /swiftshader|llvmpipe|software|basic render/i.test(renderer) };
      } catch {
        return { supported: false, renderer: null };
      }
    }

    constructor() {
      this.canvas = document.createElement('canvas');
      this.canvas.className = 'gpu-layer';
      const gl = this.canvas.getContext('webgl2', { alpha: true, antialias: false, premultipliedAlpha: false });
      if (!gl) throw new Error('WebGL2 not available');
      this.gl = gl;
      const prog = gl.createProgram();
      gl.attachShader(prog, compile(gl, gl.VERTEX_SHADER, VS));
      gl.attachShader(prog, compile(gl, gl.FRAGMENT_SHADER, FS));
      gl.linkProgram(prog);
      if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog));
      this.prog = prog;
      this.u = Object.fromEntries(['res', 'dpr', 'xMap', 'yMap', 'part', 'bodyWidth', 'maxVol', 'upColor', 'downColor']
        .map((n) => [n, gl.getUniformLocation(prog, n)]));
      this.vao = gl.createVertexArray();
      gl.bindVertexArray(this.vao);
      const quad = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, quad);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([0, 0, 1, 0, 0, 1, 0, 1, 1, 0, 1, 1]), gl.STATIC_DRAW);
      gl.enableVertexAttribArray(0);
      gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
      this.inst = gl.createBuffer();
      this.key = null;
      this.count = 0;
      this.refPrice = 0;
    }

    attach(parent) {
      if (this.canvas.parentElement !== parent) parent.insertBefore(this.canvas, parent.firstChild);
    }

    detach() {
      this.canvas.remove();
    }

    /** Upload bars when the data list changes (new symbol, interval or older page). */
    upload(bars) {
      const key = bars.length ? `${bars.length}:${bars[0].timestamp}:${bars[bars.length - 1].timestamp}:${bars[bars.length - 1].close}` : '0';
      if (key === this.key) return;
      this.key = key;
      const gl = this.gl;
      this.refPrice = bars.length ? bars[bars.length - 1].close : 0;
      const data = new Float32Array(bars.length * 6);
      for (let i = 0; i < bars.length; i++) {
        const b = bars[i], k = i * 6, r = this.refPrice;
        data[k] = i; data[k + 1] = b.open - r; data[k + 2] = b.high - r; data[k + 3] = b.low - r;
        data[k + 4] = b.close - r; data[k + 5] = b.volume || 0;
      }
      gl.bindBuffer(gl.ARRAY_BUFFER, this.inst);
      gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
      this.count = bars.length;
      this.volumes = data;
    }

    /**
     * Draw bars [from, to). mapX/mapY are the chart's exact linear pixel mappings:
     * x = x0 + i * barSpace, y = y0 + price * yScale (css px).
     */
    render({ width, height, from, to, xMap, yMap, bodyWidth, style, upColor, downColor }) {
      const gl = this.gl, dpr = window.devicePixelRatio || 1;
      const W = Math.max(1, Math.round(width * dpr)), H = Math.max(1, Math.round(height * dpr));
      if (this.canvas.width !== W || this.canvas.height !== H) {
        this.canvas.width = W; this.canvas.height = H;
        this.canvas.style.width = `${width}px`; this.canvas.style.height = `${height}px`;
      }
      gl.viewport(0, 0, W, H);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      from = Math.max(0, from); to = Math.min(this.count, to);
      const n = to - from;
      if (n <= 0) return 0;
      const t0 = performance.now();
      gl.useProgram(this.prog);
      gl.bindVertexArray(this.vao);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.inst);
      const stride = 24, base = from * stride;
      const attr = (loc, size, offset) => {
        gl.enableVertexAttribArray(loc);
        gl.vertexAttribPointer(loc, size, gl.FLOAT, false, stride, base + offset);
        gl.vertexAttribDivisor(loc, 1);
      };
      attr(1, 1, 0); attr(2, 4, 4); attr(3, 1, 20);
      let maxVol = 0;
      if (style === 'volume_candles') for (let i = from; i < to; i++) maxVol = Math.max(maxVol, this.volumes[i * 6 + 5]);
      gl.uniform2f(this.u.res, W, H);
      gl.uniform1f(this.u.dpr, dpr);
      gl.uniform2f(this.u.xMap, xMap[0], xMap[1]);
      gl.uniform2f(this.u.yMap, yMap[0] + this.refPrice * yMap[1], yMap[1]);
      gl.uniform1f(this.u.bodyWidth, bodyWidth);
      gl.uniform1f(this.u.maxVol, maxVol);
      gl.uniform4fv(this.u.upColor, parseColor(upColor));
      gl.uniform4fv(this.u.downColor, parseColor(downColor));
      const parts = style === 'bars' ? [0, 2, 3] : [0, 1];
      for (const p of parts) {
        gl.uniform1i(this.u.part, p);
        gl.drawArraysInstanced(gl.TRIANGLES, 0, 6, n);
      }
      return performance.now() - t0;
    }
  }

  window.GpuBars = GpuBars;
})();
