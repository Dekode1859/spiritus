/**
 * Graph — force-directed map of the wiki. Nodes are pages (colored by
 * folder, sized by connectivity); edges are wikilinks/backlinks.
 * Pure canvas, no dependencies: pan (drag background), zoom (wheel),
 * drag nodes, click to open the page in the Wiki view.
 */
'use strict';

import { api } from '../api.js';
import { icon } from '../icons.js';
import { escHtml, emptyState, toast } from '../ui.js';
import { state, navigate, refreshData } from '../main.js';

const NODE_COLORS = ['#22d3ee', '#a78bfa', '#f472b6', '#34d399', '#fbbf24', '#60a5fa', '#fb7185'];

const sim = {
  nodes: [],
  edges: [],
  byPath: new Map(),
  canvas: null,
  ctx: null,
  raf: 0,
  alpha: 0,
  transform: { k: 1, x: 0, y: 0 },
  hovered: null,
  dragging: null,
  panning: null,
  signature: '',
  resizeObserver: null,
};

// ── Data → simulation ────────────────────────────────────────────────────────

function folderColor(folder, folders) {
  return NODE_COLORS[Math.max(0, folders.indexOf(folder)) % NODE_COLORS.length];
}

function buildGraph() {
  const pages = state.wiki.pages || [];
  const edges = state.wiki.edges || [];
  const folders = [...new Set(pages.map(p => p.folder || ''))].sort();
  const previous = sim.byPath;

  const degree = new Map();
  for (const edge of edges) {
    degree.set(edge.source, (degree.get(edge.source) || 0) + 1);
    degree.set(edge.target, (degree.get(edge.target) || 0) + 1);
  }

  const width = sim.canvas?.clientWidth || 800;
  const height = sim.canvas?.clientHeight || 600;
  sim.nodes = pages.map((page, i) => {
    const old = previous.get(page.path);
    const angle = (i / Math.max(1, pages.length)) * Math.PI * 2;
    const spread = Math.min(width, height) * 0.32;
    return {
      path: page.path,
      title: page.title,
      folder: page.folder || '',
      color: folderColor(page.folder || '', folders),
      radius: 5 + Math.min(9, (degree.get(page.path) || 0) * 1.6),
      x: old?.x ?? width / 2 + Math.cos(angle) * spread + (Math.random() - 0.5) * 30,
      y: old?.y ?? height / 2 + Math.sin(angle) * spread + (Math.random() - 0.5) * 30,
      vx: 0,
      vy: 0,
    };
  });
  sim.byPath = new Map(sim.nodes.map(n => [n.path, n]));
  sim.edges = edges
    .map(e => ({ source: sim.byPath.get(e.source), target: sim.byPath.get(e.target) }))
    .filter(e => e.source && e.target);
  sim.alpha = 1;
}

// ── Physics ──────────────────────────────────────────────────────────────────

function step() {
  const nodes = sim.nodes;
  const width = sim.canvas.clientWidth;
  const height = sim.canvas.clientHeight;
  const cx = width / 2, cy = height / 2;
  const alpha = sim.alpha;

  // Pairwise repulsion (fine for a personal wiki's scale).
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      const a = nodes[i], b = nodes[j];
      let dx = b.x - a.x, dy = b.y - a.y;
      let d2 = dx * dx + dy * dy;
      if (d2 < 1) { dx = (Math.random() - 0.5); dy = (Math.random() - 0.5); d2 = 1; }
      const force = (2600 * alpha) / d2;
      const d = Math.sqrt(d2);
      const fx = (dx / d) * force, fy = (dy / d) * force;
      a.vx -= fx; a.vy -= fy;
      b.vx += fx; b.vy += fy;
    }
  }
  // Springs along edges.
  for (const edge of sim.edges) {
    const dx = edge.target.x - edge.source.x;
    const dy = edge.target.y - edge.source.y;
    const d = Math.sqrt(dx * dx + dy * dy) || 1;
    const stretch = (d - 110) * 0.02 * alpha;
    const fx = (dx / d) * stretch, fy = (dy / d) * stretch;
    edge.source.vx += fx; edge.source.vy += fy;
    edge.target.vx -= fx; edge.target.vy -= fy;
  }
  // Gentle gravity toward center + integration.
  for (const node of nodes) {
    node.vx += (cx - node.x) * 0.004 * alpha;
    node.vy += (cy - node.y) * 0.004 * alpha;
    if (sim.dragging === node) { node.vx = 0; node.vy = 0; continue; }
    node.vx *= 0.85; node.vy *= 0.85;
    node.x += node.vx; node.y += node.vy;
  }
  sim.alpha = Math.max(0, alpha * 0.985 - 0.0004);
}

// ── Rendering ────────────────────────────────────────────────────────────────

function draw() {
  const { ctx, canvas, transform } = sim;
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth, height = canvas.clientHeight;
  if (canvas.width !== width * dpr || canvas.height !== height * dpr) {
    canvas.width = width * dpr;
    canvas.height = height * dpr;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  ctx.translate(transform.x, transform.y);
  ctx.scale(transform.k, transform.k);

  const neighbors = new Set();
  if (sim.hovered) {
    neighbors.add(sim.hovered);
    for (const e of sim.edges) {
      if (e.source === sim.hovered) neighbors.add(e.target);
      if (e.target === sim.hovered) neighbors.add(e.source);
    }
  }

  for (const edge of sim.edges) {
    const active = sim.hovered && (edge.source === sim.hovered || edge.target === sim.hovered);
    ctx.strokeStyle = active ? 'rgba(34, 211, 238, 0.55)' : 'rgba(139, 152, 171, 0.16)';
    ctx.lineWidth = active ? 1.4 : 1;
    ctx.beginPath();
    ctx.moveTo(edge.source.x, edge.source.y);
    ctx.lineTo(edge.target.x, edge.target.y);
    ctx.stroke();
  }

  const showAllLabels = sim.nodes.length <= 40 && transform.k > 0.55;
  for (const node of sim.nodes) {
    const dimmed = sim.hovered && !neighbors.has(node);
    ctx.globalAlpha = dimmed ? 0.25 : 1;
    if (!dimmed) {
      ctx.shadowColor = node.color;
      ctx.shadowBlur = node === sim.hovered ? 18 : 8;
    }
    ctx.fillStyle = node.color;
    ctx.beginPath();
    ctx.arc(node.x, node.y, node.radius, 0, Math.PI * 2);
    ctx.fill();
    ctx.shadowBlur = 0;
    ctx.fillStyle = '#0b0e14';
    ctx.beginPath();
    ctx.arc(node.x, node.y, Math.max(1.5, node.radius - 2.2), 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = node.color;
    ctx.beginPath();
    ctx.arc(node.x, node.y, Math.max(1, node.radius - 3.6), 0, Math.PI * 2);
    ctx.fill();

    if (showAllLabels || neighbors.has(node)) {
      ctx.font = `${node === sim.hovered ? 600 : 400} 11px "Segoe UI", system-ui, sans-serif`;
      ctx.fillStyle = node === sim.hovered ? '#e6edf6' : 'rgba(230, 237, 246, 0.72)';
      ctx.textAlign = 'center';
      ctx.fillText(node.title.length > 34 ? node.title.slice(0, 33) + '…' : node.title,
        node.x, node.y + node.radius + 13);
    }
    ctx.globalAlpha = 1;
  }
}

function loop() {
  if (!sim.canvas?.isConnected) return;
  if (sim.alpha > 0.008) step();
  draw();
  sim.raf = requestAnimationFrame(loop);
}

// ── Interaction ──────────────────────────────────────────────────────────────

function toWorld(event) {
  const rect = sim.canvas.getBoundingClientRect();
  const { k, x, y } = sim.transform;
  return {
    x: (event.clientX - rect.left - x) / k,
    y: (event.clientY - rect.top - y) / k,
  };
}

function nodeAt(point) {
  for (let i = sim.nodes.length - 1; i >= 0; i--) {
    const node = sim.nodes[i];
    const dx = point.x - node.x, dy = point.y - node.y;
    if (dx * dx + dy * dy <= (node.radius + 4) ** 2) return node;
  }
  return null;
}

function fitView() {
  if (!sim.nodes.length) return;
  const xs = sim.nodes.map(n => n.x), ys = sim.nodes.map(n => n.y);
  const minX = Math.min(...xs) - 60, maxX = Math.max(...xs) + 60;
  const minY = Math.min(...ys) - 60, maxY = Math.max(...ys) + 60;
  const width = sim.canvas.clientWidth, height = sim.canvas.clientHeight;
  const k = Math.min(2, Math.min(width / (maxX - minX), height / (maxY - minY)));
  sim.transform = {
    k,
    x: width / 2 - k * (minX + maxX) / 2,
    y: height / 2 - k * (minY + maxY) / 2,
  };
}

function wireCanvas() {
  const canvas = sim.canvas;
  let moved = false;

  canvas.addEventListener('pointerdown', event => {
    const point = toWorld(event);
    const node = nodeAt(point);
    moved = false;
    if (node) {
      sim.dragging = node;
      sim.alpha = Math.max(sim.alpha, 0.35);
    } else {
      sim.panning = { startX: event.clientX, startY: event.clientY, ...sim.transform };
    }
    canvas.setPointerCapture(event.pointerId);
  });

  canvas.addEventListener('pointermove', event => {
    if (sim.dragging) {
      const point = toWorld(event);
      sim.dragging.x = point.x;
      sim.dragging.y = point.y;
      sim.alpha = Math.max(sim.alpha, 0.25);
      moved = true;
      return;
    }
    if (sim.panning) {
      sim.transform.x = sim.panning.x + (event.clientX - sim.panning.startX);
      sim.transform.y = sim.panning.y + (event.clientY - sim.panning.startY);
      moved = true;
      return;
    }
    const node = nodeAt(toWorld(event));
    if (node !== sim.hovered) {
      sim.hovered = node;
      canvas.style.cursor = node ? 'pointer' : 'grab';
      const tip = document.getElementById('graph-tip');
      if (node) {
        tip.hidden = false;
        tip.innerHTML = `<strong>${escHtml(node.title)}</strong>${node.folder ? `<span>${escHtml(node.folder)}</span>` : ''}`;
      } else {
        tip.hidden = true;
      }
    }
    if (sim.hovered) {
      const tip = document.getElementById('graph-tip');
      const rect = canvas.getBoundingClientRect();
      tip.style.left = `${event.clientX - rect.left + 14}px`;
      tip.style.top = `${event.clientY - rect.top + 14}px`;
    }
  });

  canvas.addEventListener('pointerup', event => {
    const wasNode = sim.dragging;
    sim.dragging = null;
    sim.panning = null;
    canvas.releasePointerCapture(event.pointerId);
    if (wasNode && !moved) navigate('wiki', { path: wasNode.path });
  });

  canvas.addEventListener('wheel', event => {
    event.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const mx = event.clientX - rect.left, my = event.clientY - rect.top;
    const factor = event.deltaY < 0 ? 1.12 : 1 / 1.12;
    const k = Math.min(3, Math.max(0.2, sim.transform.k * factor));
    sim.transform.x = mx - (mx - sim.transform.x) * (k / sim.transform.k);
    sim.transform.y = my - (my - sim.transform.y) * (k / sim.transform.k);
    sim.transform.k = k;
  }, { passive: false });
}

// ── View module ──────────────────────────────────────────────────────────────

function legendHtml() {
  const pages = state.wiki.pages || [];
  const folders = [...new Set(pages.map(p => p.folder || ''))].sort();
  return folders.map(folder => `
    <span class="legend-item">
      <span class="legend-dot" style="background:${folderColor(folder, folders)}"></span>
      ${escHtml(folder || 'Pages')}
    </span>`).join('');
}

export const graphView = {
  id: 'graph',
  title: 'Graph',
  icon: 'waypoints',

  render(root) {
    const pages = state.wiki.pages || [];
    root.innerHTML = `
      <header class="view-header">
        <div>
          <h1>Graph</h1>
          <p class="view-sub">${pages.length} pages · ${(state.wiki.edges || []).length} connections</p>
        </div>
        <div class="view-actions">
          <button class="btn btn-ghost" id="graph-reconcile" title="Purge deleted sources and rebuild the graph from what remains">${icon('refresh-cw')}Refresh</button>
          <button class="btn btn-ghost" id="graph-shuffle" title="Re-run layout">${icon('waypoints')}Layout</button>
          <button class="btn btn-ghost" id="graph-fit" title="Fit to view">${icon('maximize')}Fit</button>
        </div>
      </header>
      <div class="graph-stage">
        <canvas id="graph-canvas"></canvas>
        <div class="graph-tip" id="graph-tip" hidden></div>
        <div class="graph-legend">${legendHtml()}</div>
        ${!pages.length ? `<div class="graph-empty">${emptyState('waypoints', 'Nothing to map yet', 'When agents write linked wiki pages, the knowledge graph appears here.')}</div>` : ''}
      </div>`;

    sim.canvas = root.querySelector('#graph-canvas');
    sim.ctx = sim.canvas.getContext('2d');
    sim.transform = { k: 1, x: 0, y: 0 };
    sim.signature = JSON.stringify([pages.map(p => p.path), state.wiki.edges]);
    buildGraph();
    wireCanvas();

    root.querySelector('#graph-shuffle').addEventListener('click', () => {
      sim.byPath = new Map();
      buildGraph();
      setTimeout(fitView, 350);
    });
    root.querySelector('#graph-fit').addEventListener('click', fitView);
    root.querySelector('#graph-reconcile').addEventListener('click', async (event) => {
      const btn = event.currentTarget;
      btn.disabled = true;
      try {
        const result = await api.reconcile();
        if (!result.ok) {
          toast('err', result.error || 'Refresh failed.');
          return;
        }
        const purged = (result.removed_notes || 0) + (result.removed_manifests || 0);
        toast('ok', purged
          ? `Removed ${purged} orphaned item${purged === 1 ? '' : 's'} from deleted sources.`
          : 'Graph is already up to date.');
        await refreshData({ silent: true });
        this.render(root);   // rebuild with the cleaned data
      } catch (error) {
        toast('err', error.message || 'Refresh failed.');
      } finally {
        btn.disabled = false;
      }
    });

    sim.resizeObserver = new ResizeObserver(() => draw());
    sim.resizeObserver.observe(sim.canvas);

    cancelAnimationFrame(sim.raf);
    loop();
    setTimeout(fitView, 500);
  },

  update() {
    const root = document.getElementById('view');
    if (!root || root.dataset.view !== 'graph') return;
    const signature = JSON.stringify([(state.wiki.pages || []).map(p => p.path), state.wiki.edges]);
    if (signature !== sim.signature) {
      this.render(root);
    }
  },

  destroy() {
    cancelAnimationFrame(sim.raf);
    sim.raf = 0;
    sim.resizeObserver?.disconnect();
    sim.resizeObserver = null;
    sim.hovered = null;
    sim.dragging = null;
    sim.panning = null;
  },
};
