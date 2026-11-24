'use strict';

function graphSlice(items) {
  const nodes = new Map(), edges = [];
  for (const item of items) {
    if (item.kind === 'entity') nodes.set(item.source, item);
    else {
      edges.push(item);
      for (const node of [item.subject, item.object]) {
        if (!nodes.has(node.source)) nodes.set(node.source, node);
      }
    }
  }
  const adjacent = new Map([...nodes.keys()].map(source => [source, new Set()]));
  const groups = new Map();
  for (const edge of edges) {
    const subject = edge.subject.source, object = edge.object.source;
    adjacent.get(subject).add(object);
    adjacent.get(object).add(subject);
    const pair = JSON.stringify([subject, object].sort());
    if (!groups.has(pair)) groups.set(pair, []);
    groups.get(pair).push(edge);
  }
  const order = new Set([...nodes.keys()].sort());
  const ordered = [];
  for (const root of order) {
    const component = [root];
    order.delete(root);
    for (const source of component) {
      for (const neighbor of [...adjacent.get(source)].sort()) {
        if (order.delete(neighbor)) component.push(neighbor);
      }
    }
    ordered.push(...component);
  }
  return {nodes, edges, adjacent, groups, ordered};
}

function createMemoryGraphCanvas({el, open, label}) {
  const cellWidth = 156, cellHeight = 114, nodeRadius = 17, margin = 67;
  const minZoom = 0.75, maxZoom = 2, zoomStep = 0.25;
  function svg(tag, attributes = {}, text) {
    const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
    if (text !== undefined) node.textContent = text;
    return node;
  }
  const picture = svg('svg', {role:'group', 'aria-label':'当前页关系网络'});
  const viewport = el('div', {class:'memory-graph-viewport', tabindex:'0', role:'region', 'aria-label':'关系网络滚动区域'}, picture);
  const description = el('p', {class:'muted', id:'memory-graph-caption'});
  const reduce = el('button', {class:'btn small', type:'button', text:'缩小', onclick:() => setZoom(zoom - zoomStep)});
  const enlarge = el('button', {class:'btn small', type:'button', text:'放大', onclick:() => setZoom(zoom + zoomStep)});
  const reset = el('button', {class:'btn small', type:'button', text:'复位', onclick:resetView});
  const toggle = el('button', {class:'btn small', type:'button', text:'隐藏关系图', 'aria-expanded':'true',
    'aria-controls':'memory-graph-plot', onclick:() => setPlotVisible(plot.classList.contains('hidden'))});
  const plot = el('div', {id:'memory-graph-plot', class:'stack'},
    el('div', {class:'row', role:'group', 'aria-label':'关系图缩放'}, reduce, enlarge, reset), viewport);
  const element = el('section', {class:'memory-graph-canvas stack hidden', 'aria-label':'当前页关系图'},
    el('div', {class:'row'}, description, toggle), plot);
  let zoom = 1, width = 0, height = 0, drawn = null;

  function setPlotVisible(visible) {
    plot.classList.toggle('hidden', !visible);
    toggle.setAttribute('aria-expanded', visible);
    toggle.textContent = visible ? '隐藏关系图' : '显示关系图';
  }
  function setZoom(value) {
    zoom = value;
    picture.style.width = `${width * zoom}px`;
    picture.style.height = `${height * zoom}px`;
    reduce.disabled = zoom <= minZoom;
    enlarge.disabled = zoom >= maxZoom;
  }
  function resetView() {
    drawn?.reset();
    viewport.scrollLeft = viewport.scrollTop = 0;
    setZoom(1);
  }
  function draw(items) {
    const {nodes, edges, adjacent, groups, ordered} = graphSlice(items);
    if (!nodes.size) return;
    element.classList.remove('hidden');
    description.textContent = `当前页绘制 ${nodes.size} 个节点、${edges.length} 条有向关系（含随关系返回的端点）。箭头指向关系终点。`;

    const columns = Math.min(5, Math.ceil(Math.sqrt(nodes.size)));
    width = cellWidth * columns + margin * 2;
    height = cellHeight * Math.ceil(nodes.size / columns) + margin * 2;
    picture.setAttribute('viewBox', `0 0 ${width} ${height}`);
    picture.setAttribute('aria-describedby', 'memory-graph-caption');

    const layouts = new Map(ordered.map((source, index) => {
      const row = Math.floor(index / columns), slot = index % columns;
      const column = row % 2 ? columns - slot - 1 : slot;
      const x = margin + (column + 0.5) * cellWidth, y = margin + (row + 0.5) * cellHeight;
      return [source, {x, y, initialX:x, initialY:y}];
    }));
    const marker = svg('marker', {id:'memory-graph-arrow', markerWidth:7, markerHeight:7, refX:6, refY:3.5,
      orient:'auto', viewBox:'0 0 7 7'});
    marker.append(svg('path', {d:'M 0 0 L 7 3.5 L 0 7 Z'}));
    const definitions = svg('defs'), edgeLayer = svg('g'), nodeLayer = svg('g');
    definitions.append(marker);
    picture.replaceChildren(definitions, edgeLayer, nodeLayer);
    const nodeViews = new Map(), edgeViews = [];
    const nameCounts = new Map();
    for (const node of nodes.values()) nameCounts.set(node.name, (nameCounts.get(node.name) ?? 0) + 1);
    const captions = new Map(ordered.map((source, index) => {
      const name = nodes.get(source).name;
      return [source, nameCounts.get(name) > 1 ? `${name}（${index + 1}）` : name];
    }));
    let selected = null, hovered = null;

    function highlight() {
      const active = hovered ?? selected;
      const neighbors = adjacent.get(active);
      for (const [source, view] of nodeViews) {
        view.classList.toggle('is-selected', source === selected);
        view.classList.toggle('is-muted', Boolean(active) && source !== active && !neighbors?.has(source));
      }
      for (const {edge, view} of edgeViews) {
        const incident = edge.subject.source === active || edge.object.source === active;
        view.classList.toggle('is-neighbor', incident);
        view.classList.toggle('is-muted', Boolean(active) && !incident);
      }
    }
    function choose(source) {
      selected = source;
      highlight();
    }
    function keys(view, item, select) {
      view.addEventListener('click', () => { select?.(); open(item.source); });
      view.addEventListener('keydown', event => {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        event.preventDefault();
        select?.();
        open(item.source);
      });
    }
    const short = (text, max) => {
      const letters = Array.from(text);
      return letters.length > max ? letters.slice(0, max).join('') + '…' : text;
    };
    for (const group of groups.values()) {
      const sorted = [...group].sort((left, right) => left.source < right.source ? -1 : left.source > right.source ? 1 : 0);
      for (const [index, edge] of sorted.entries()) {
        const view = svg('g', {class:'memory-graph-edge', role:'button', tabindex:0, 'aria-label':label(edge)});
        const path = svg('path', {'marker-end':'url(#memory-graph-arrow)'});
        const hit = svg('path', {class:'memory-graph-edge-hit'});
        const caption = svg('text', {'text-anchor':'middle'}, short(edge.predicate, 10));
        view.append(svg('title', {}, label(edge)), path, hit, caption);
        edgeLayer.append(view);
        edgeViews.push({edge, view, path, hit, caption, index, count:sorted.length});
        keys(view, edge);
      }
    }
    edgeLayer.classList.toggle('is-dense', edges.length > 8);

    function place() {
      for (const [source, view] of nodeViews) {
        const {x, y} = layouts.get(source);
        view.setAttribute('transform', `translate(${x} ${y})`);
      }
      for (const record of edgeViews) {
        const {x, y} = layouts.get(record.edge.subject.source);
        const target = layouts.get(record.edge.object.source);
        const {path, hit, caption, index, count} = record;
        let curve, labelX, labelY;
        if (record.edge.subject.source === record.edge.object.source) {
          const reach = nodeRadius + 16 + 30 * index / count;
          curve = `M ${x - 11} ${y - 15} C ${x - reach} ${y - reach * 1.6}, ${x + reach} ${y - reach * 1.6}, ${x + 11} ${y - 17}`;
          labelX = x;
          labelY = y - reach * 1.2;
        } else {
          const dx = target.x - x, dy = target.y - y;
          const distance = Math.hypot(dx, dy) || 1;
          const ux = dx / distance, uy = dy / distance;
          const sign = record.edge.subject.source < record.edge.object.source ? 1 : -1;
          const shift = sign * (index - (count - 1) / 2) * Math.min(26, 210 / count);
          const cx = (x + target.x) / 2 - uy * shift, cy = (y + target.y) / 2 + ux * shift;
          const first = Math.hypot(cx - x, cy - y) || 1;
          const last = Math.hypot(cx - target.x, cy - target.y) || 1;
          const startX = x + (cx - x) * nodeRadius / first, startY = y + (cy - y) * nodeRadius / first;
          const endX = target.x + (cx - target.x) * (nodeRadius + 2) / last;
          const endY = target.y + (cy - target.y) * (nodeRadius + 2) / last;
          curve = `M ${startX} ${startY} Q ${cx} ${cy} ${endX} ${endY}`;
          labelX = (startX + 2 * cx + endX) / 4;
          labelY = (startY + 2 * cy + endY) / 4;
        }
        path.setAttribute('d', curve);
        hit.setAttribute('d', curve);
        caption.setAttribute('x', labelX);
        caption.setAttribute('y', labelY - 5);
      }
    }
    for (const source of ordered) {
      const node = nodes.get(source), name = captions.get(source);
      const view = svg('g', {class:'memory-graph-node', role:'button', tabindex:0, 'aria-label':`实体 · ${name}`});
      const visibleName = name === node.name ? short(name, 10) : short(node.name, 8) + name.slice(node.name.length);
      view.append(svg('title', {}, name), svg('circle', {r:nodeRadius}),
        svg('text', {y:nodeRadius + 16, 'text-anchor':'middle'}, visibleName));
      nodeLayer.append(view);
      nodeViews.set(source, view);
      keys(view, node, () => choose(source));
      view.addEventListener('pointerenter', () => { hovered = source; highlight(); });
      view.addEventListener('pointerleave', () => { hovered = null; highlight(); });
      view.addEventListener('focus', () => { hovered = source; highlight(); });
      view.addEventListener('blur', () => { hovered = null; highlight(); });
      let drag;
      view.addEventListener('pointerdown', event => {
        if (event.button !== 0) return;
        const {x, y} = layouts.get(source);
        drag = {pointer:event.pointerId, x, y, clientX:event.clientX, clientY:event.clientY, moved:false};
        view.setPointerCapture(event.pointerId);
      });
      view.addEventListener('pointermove', event => {
        if (!drag || drag.pointer !== event.pointerId) return;
        const dx = event.clientX - drag.clientX, dy = event.clientY - drag.clientY;
        if (!drag.moved && Math.hypot(dx, dy) < 5) return;
        drag.moved = true;
        const position = layouts.get(source);
        position.x = Math.max(margin, Math.min(width - margin, drag.x + dx / zoom));
        position.y = Math.max(margin, Math.min(height - margin, drag.y + dy / zoom));
        place();
      });
      view.addEventListener('pointerup', event => {
        if (!drag || drag.pointer !== event.pointerId) return;
        if (drag.moved) {
          choose(source);
          view.focus();
        }
      });
      view.addEventListener('pointercancel', () => { drag = null; });
      view.addEventListener('click', event => {
        const moved = drag?.moved;
        drag = null;
        if (moved && event.detail) event.stopImmediatePropagation();
      }, {capture:true});
    }
    drawn = {reset() {
      for (const position of layouts.values()) {
        position.x = position.initialX;
        position.y = position.initialY;
      }
      hovered = null;
      choose(null);
      place();
    }};
    resetView();
  }

  return {element, draw, clear() {
    drawn = null;
    description.textContent = '';
    picture.replaceChildren();
    element.classList.add('hidden');
  }};
}
