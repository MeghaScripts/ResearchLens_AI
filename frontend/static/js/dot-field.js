// dot-field.js — ambient floating dots across the whole page; tap/click one and it bursts and vanishes
(function () {
  const canvas = document.createElement('canvas');
  canvas.id = 'dotField';
  canvas.style.cssText = 'position:fixed;inset:0;z-index:2;pointer-events:none;';
  document.body.appendChild(canvas);
  const ctx = canvas.getContext('2d');

  const COLORS = ['#00d4ff', '#7c3aed', '#06ffa5', '#ff9f43'];
  const DOT_COUNT = 14;

  let dots = [];
  let bursts = [];
  let W = window.innerWidth, H = window.innerHeight;

  function resize() {
    W = window.innerWidth; H = window.innerHeight;
    canvas.width = W * devicePixelRatio;
    canvas.height = H * devicePixelRatio;
    canvas.style.width = W + 'px';
    canvas.style.height = H + 'px';
    ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
  }
  window.addEventListener('resize', resize);
  resize();

  function rand(a, b) { return a + Math.random() * (b - a); }

  function spawnDot() {
    return {
      x: rand(0, W), y: rand(0, H),
      vx: rand(-0.12, 0.12), vy: rand(-0.12, 0.12),
      baseR: rand(1.6, 3.4),
      depthPhase: rand(0, Math.PI * 2),
      depthSpeed: rand(0.006, 0.016),
      color: COLORS[Math.floor(Math.random() * COLORS.length)],
      alphaBase: rand(0.25, 0.55)
    };
  }

  for (let i = 0; i < DOT_COUNT; i++) dots.push(spawnDot());

  function burstAt(x, y, color) {
    const n = 10 + Math.floor(Math.random() * 6);
    for (let i = 0; i < n; i++) {
      const angle = (Math.PI * 2 * i) / n + rand(-0.2, 0.2);
      const speed = rand(1.2, 3.2);
      bursts.push({
        x, y,
        vx: Math.cos(angle) * speed,
        vy: Math.sin(angle) * speed,
        r: rand(1.5, 3),
        life: 1,
        decay: rand(0.02, 0.035),
        color
      });
    }
  }

  function hitTest(x, y) {
    for (let i = 0; i < dots.length; i++) {
      const d = dots[i];
      const depth = 0.6 + 0.4 * Math.sin(d.depthPhase);
      const r = d.baseR * depth;
      const dx = d.x - x, dy = d.y - y;
      if (dx * dx + dy * dy <= (r + 10) * (r + 10)) return i;
    }
    return -1;
  }

  function handlePoint(clientX, clientY) {
    const idx = hitTest(clientX, clientY);
    if (idx === -1) return;
    const d = dots[idx];
    burstAt(d.x, d.y, d.color);
    dots.splice(idx, 1);
    // respawn a fresh dot elsewhere after a short pause so density stays steady
    setTimeout(() => dots.push(spawnDot()), rand(400, 1200));
  }

  document.addEventListener('click', (e) => handlePoint(e.clientX, e.clientY), { passive: true });
  document.addEventListener('touchstart', (e) => {
    if (!e.touches || !e.touches.length) return;
    const t = e.touches[0];
    handlePoint(t.clientX, t.clientY);
  }, { passive: true });

  function draw() {
    requestAnimationFrame(draw);
    ctx.clearRect(0, 0, W, H);

    dots.forEach(d => {
      d.x += d.vx; d.y += d.vy;
      d.depthPhase += d.depthSpeed;
      if (d.x < -10) d.x = W + 10; if (d.x > W + 10) d.x = -10;
      if (d.y < -10) d.y = H + 10; if (d.y > H + 10) d.y = -10;

      const depth = 0.6 + 0.4 * Math.sin(d.depthPhase);
      const r = d.baseR * depth;
      const alpha = d.alphaBase * depth;

      const grad = ctx.createRadialGradient(d.x, d.y, 0, d.x, d.y, r * 4);
      grad.addColorStop(0, hexToRgba(d.color, alpha));
      grad.addColorStop(1, hexToRgba(d.color, 0));
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.arc(d.x, d.y, r * 4, 0, Math.PI * 2);
      ctx.fill();

      ctx.fillStyle = hexToRgba(d.color, Math.min(1, alpha + 0.25));
      ctx.beginPath();
      ctx.arc(d.x, d.y, r, 0, Math.PI * 2);
      ctx.fill();
    });

    bursts = bursts.filter(b => b.life > 0);
    bursts.forEach(b => {
      b.x += b.vx; b.y += b.vy;
      b.vx *= 0.95; b.vy *= 0.95;
      b.life -= b.decay;
      ctx.fillStyle = hexToRgba(b.color, Math.max(0, b.life));
      ctx.beginPath();
      ctx.arc(b.x, b.y, b.r * b.life, 0, Math.PI * 2);
      ctx.fill();
    });
  }

  function hexToRgba(hex, a) {
    const n = parseInt(hex.slice(1), 16);
    const r = (n >> 16) & 255, g = (n >> 8) & 255, bl = n & 255;
    return `rgba(${r},${g},${bl},${a})`;
  }

  draw();
})();