/*
  New Year effects:
  - Canvas fireworks overlay
  - Falling “Happy New Year YYYY!” text every 10 seconds

  Runs only when base.html injects this file.
*/
(function () {
  'use strict';
  const enteringYear = (typeof window.NEWYEAR_ENTERING_YEAR === 'number')
    ? window.NEWYEAR_ENTERING_YEAR
    : (new Date().getFullYear() + 1);
  // Bail out for old browsers with no canvas support.
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext && canvas.getContext('2d');
  if (!ctx) return;
  canvas.id = 'ny-fireworks-canvas';
  document.body.appendChild(canvas);
  const textLayer = document.createElement('div');
  textLayer.id = 'ny-text-layer';
  document.body.appendChild(textLayer);
  // --- Resize handling ---
  let DPR = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
  function resize() {
    const w = window.innerWidth;
    const h = window.innerHeight;
    DPR = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
    canvas.width = Math.floor(w * DPR);
    canvas.height = Math.floor(h * DPR);
    canvas.style.width = w + 'px';
    canvas.style.height = h + 'px';
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  }
  resize();
  window.addEventListener('resize', resize, { passive: true });
  // --- Fireworks model ---
  const particles = [];
  const rockets = [];
  function rand(min, max) {
    return min + Math.random() * (max - min);
  }
  function hsla(h, s, l, a) {
    return `hsla(${h}, ${s}%, ${l}%, ${a})`;
  }
  function spawnExplosion(x, y, power) {
    const count = Math.floor(rand(60, 110) * power);
    const baseHue = rand(0, 360);
    const hueJitter = rand(25, 80);
    for (let i = 0; i < count; i++) {
      const angle = rand(0, Math.PI * 2);
      const speed = rand(1.5, 6.5) * power;
      const vx = Math.cos(angle) * speed;
      const vy = Math.sin(angle) * speed;
      const hue = (baseHue + rand(-hueJitter, hueJitter) + 360) % 360;
      const life = rand(50, 110);
      particles.push({
        x,
        y,
        vx,
        vy,
        life,
        maxLife: life,
        size: rand(1.0, 2.6) * (0.8 + power * 0.6),
        hue,
        sparkle: Math.random() < 0.18,
      });
    }
    // A little extra “flash” ring to sell it.
    particles.push({
      ring: true,
      x,
      y,
      r: 0,
      vr: rand(6, 12) * power,
      life: 18,
      maxLife: 18,
      hue: baseHue,
    });
  }

  function spawnRocket() {
    const w = window.innerWidth;
    const h = window.innerHeight;
    const x = rand(w * 0.1, w * 0.9);
    const y = h + rand(20, 60);
    rockets.push({
      x,
      y,
      vx: rand(-0.6, 0.6),
      vy: rand(-11, -15),
      life: rand(55, 85),
      hue: rand(0, 360),
      trail: [],
    });
  }

  // Keep it “spectacular” without melting phones.
  const MAX_PARTICLES = 1400;

  function step() {
    const w = window.innerWidth;
    const h = window.innerHeight;

    // Fade previous frame to TRANSPARENT (keeps the page visible underneath).
    ctx.globalCompositeOperation = 'destination-out';
    ctx.fillStyle = 'rgba(0,0,0,0.18)';
    ctx.fillRect(0, 0, w, h);

// Additive blending for fireworks
ctx.globalCompositeOperation = 'lighter';

    // Additive blending for glow.
    ctx.globalCompositeOperation = 'lighter';

    // Spawn rockets occasionally.
    if (Math.random() < 0.08) spawnRocket();
    if (Math.random() < 0.02) {
      // Random mid-air burst for variety.
      spawnExplosion(rand(w * 0.15, w * 0.85), rand(h * 0.12, h * 0.55), rand(0.7, 1.2));
    }

    // Update rockets.
    for (let i = rockets.length - 1; i >= 0; i--) {
      const r = rockets[i];
      r.life -= 1;

      r.x += r.vx;
      r.y += r.vy;
      r.vy += 0.18; // gravity

      // Trail points
      r.trail.push({ x: r.x, y: r.y, a: 1 });
      if (r.trail.length > 14) r.trail.shift();

      // Draw trail
      for (let t = 0; t < r.trail.length; t++) {
        const p = r.trail[t];
        const alpha = (t / r.trail.length) * 0.7;
        ctx.fillStyle = hsla(r.hue, 90, 65, alpha);
        ctx.beginPath();
        ctx.arc(p.x, p.y, 1.4 + t * 0.05, 0, Math.PI * 2);
        ctx.fill();
      }

      // Draw rocket head
      ctx.fillStyle = hsla(r.hue, 100, 70, 0.9);
      ctx.beginPath();
      ctx.arc(r.x, r.y, 2.2, 0, Math.PI * 2);
      ctx.fill();

      const shouldExplode = (r.life <= 0) || (r.vy > -2.5) || (r.y < h * 0.18);
      if (shouldExplode) {
        spawnExplosion(r.x, r.y, rand(0.8, 1.35));
        rockets.splice(i, 1);
      }
    }

    // Update particles.
    for (let i = particles.length - 1; i >= 0; i--) {
      const p = particles[i];
      p.life -= 1;
      if (p.life <= 0) {
        particles.splice(i, 1);
        continue;
      }

      const t = p.life / p.maxLife;

      if (p.ring) {
        p.r += p.vr;
        p.vr *= 0.88;
        ctx.strokeStyle = hsla(p.hue, 100, 75, 0.28 * t);
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
        ctx.stroke();
        continue;
      }

      p.x += p.vx;
      p.y += p.vy;

      // Gravity + drag
      p.vy += 0.14;
      p.vx *= 0.985;
      p.vy *= 0.985;

      // Bounce a tiny bit off bottom to keep it lively.
      if (p.y > h + 20) {
        p.y = h + 20;
        p.vy *= -0.35;
      }

      const alpha = Math.max(0, Math.min(1, t));
      const light = 55 + (1 - t) * 20;

      // Sparkles flicker
      const flicker = p.sparkle ? (0.6 + Math.random() * 0.4) : 1;
      ctx.fillStyle = hsla(p.hue, 100, light, alpha * 0.85 * flicker);
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2);
      ctx.fill();

      // Tiny star cross for some particles
      if (p.sparkle && Math.random() < 0.35) {
        ctx.strokeStyle = hsla(p.hue, 100, 75, alpha * 0.55);
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(p.x - 3, p.y);
        ctx.lineTo(p.x + 3, p.y);
        ctx.moveTo(p.x, p.y - 3);
        ctx.lineTo(p.x, p.y + 3);
        ctx.stroke();
      }

      // Cull far off-screen
      if (p.x < -200 || p.x > w + 200 || p.y < -200 || p.y > h + 300) {
        particles.splice(i, 1);
      }
    }

    // Hard cap so it doesn’t become a browser murder-suicide.
    if (particles.length > MAX_PARTICLES) {
      particles.splice(0, particles.length - MAX_PARTICLES);
    }

    requestAnimationFrame(step);
  }

  // Kick off with a couple of immediate bursts.
  document.addEventListener('DOMContentLoaded', () => {
    const w = window.innerWidth;
    const h = window.innerHeight;
    spawnExplosion(rand(w * 0.2, w * 0.8), rand(h * 0.18, h * 0.5), 1.1);
    spawnExplosion(rand(w * 0.2, w * 0.8), rand(h * 0.18, h * 0.5), 1.0);
    step();
  });

  // --- Falling text ---
  function spawnFallingText() {
    const node = document.createElement('div');
    node.className = 'ny-falling';
    node.textContent = `Happy New Year ${enteringYear}!`;

    // Random X, and a size that scales a bit with viewport.
    const x = rand(5, 85);
    const base = Math.max(18, Math.min(44, window.innerWidth / 28));
    const size = base + rand(-4, 10);

    node.style.left = x + 'vw';
    node.style.fontSize = size + 'px';

    // Funky gradient-ish color via hue rotation.
    const hue = rand(0, 360);
    node.style.color = hsla(hue, 100, 70, 0.95);

    textLayer.appendChild(node);

    // Clean up after animation.
    node.addEventListener('animationend', () => {
      if (node && node.parentNode) node.parentNode.removeChild(node);
    });

    // Fallback cleanup.
    setTimeout(() => {
      if (node && node.parentNode) node.parentNode.removeChild(node);
    }, 9000);
  }

  // Every 10 seconds (as requested), but also do one quickly so it’s obvious.
  setTimeout(spawnFallingText, 1200);
  setInterval(spawnFallingText, 10000);
})();
