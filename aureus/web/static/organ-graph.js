/* 器官图只呈现逻辑流向。模块位置属于当前浏览器，不写入运行配置。 */
'use strict';

// 在静止的命中框上测距，倾斜只作用于内层块面。
class PixelField {
  constructor(page, render) {
    this.pointer = null;
    this.frame = null;
    this.render = render;
    const motion = matchMedia('(prefers-reduced-motion: reduce)');
    const clear = () => {
      if (!this.pointer) return;
      this.pointer = null;
      this.refresh();
    };
    const track = event => {
      if (!event.isPrimary) return;
      if (event.pointerType === 'touch' || motion.matches) { clear(); return; }
      this.pointer = {x:event.clientX, y:event.clientY};
      this.refresh();
    };
    page.addEventListener('pointerover', track);
    page.addEventListener('pointermove', track);
    page.addEventListener('pointerleave', clear);
    page.addEventListener('pointercancel', clear);
    page.addEventListener('scroll', () => { if (this.pointer) this.refresh(); }, {passive:true});
    window.addEventListener('blur', clear);
    motion.addEventListener('change', clear);
  }
  refresh() {
    if (this.frame !== null) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = null;
      this.render(this.pointer);
    });
  }
    static tiltAt(node, x, y, maxTilt) {
    const dx = x - node.x, dy = y - node.y;
    const distance = Math.hypot(Math.max(0, Math.abs(dx) - node.halfWidth),
                                Math.max(0, Math.abs(dy) - node.halfHeight));
    const fieldRadius = 480;  // 感应距离从块边缘量起，单位为 CSS 像素。
    if (distance >= fieldRadius) return '';
    const t = 1 - distance / fieldRadius;
    const horizontal = dx / node.halfWidth, vertical = dy / node.halfHeight;
    // 边缘处平滑达到最大倾角；进入块面后，靠近中心时逐渐压平。
    const angle = maxTilt * t * t * (3 - 2 * t) / Math.max(1, Math.hypot(horizontal, vertical));
    return 'rotateX('+(-vertical*angle).toFixed(2)+'deg) rotateY('+(horizontal*angle).toFixed(2)+'deg)';
  }
}

const OrganFlowView = (() => {
  const namespace = 'http://www.w3.org/2000/svg';
  const layoutKey = 'aureus.organ-layout.v1';
  const clamp = (n, min, max) => Math.max(min, Math.min(max, n));
  const titleOf = id => id === null ? 'CORE' : id.toUpperCase();
  const svg = (tag, attrs = {}) => {
    const node = document.createElementNS(namespace, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    return node;
  };

  // 插口落在方块的直边上，离阶梯形圆角留出两个方向的插口间距。
  function socket(node, ux, uy, direction, offset) {
    const {x, y, halfWidth, halfHeight} = node;
    const corner = 7;
    if (Math.abs(ux) * halfHeight >= Math.abs(uy) * halfWidth) {
      const side = direction * Math.sign(ux);
      const limit = Math.max(0, halfHeight - corner - offset);
      const along = clamp(side * halfWidth * uy / ux, -limit, limit);
      return [x + side * halfWidth, y + along + Math.sign(ux) * offset];
    }
    const side = direction * Math.sign(uy);
    const limit = Math.max(0, halfWidth - corner - offset);
    const along = clamp(side * halfHeight * ux / uy, -limit, limit);
    return [x + along - Math.sign(uy) * offset, y + side * halfHeight];
  }

  function cable(a, b, kind, core) {
    if (a===b) {
      const side=a.x>a.bounds[0]+a.bounds[2]/2?-1:1, x=a.x+side*a.halfWidth, y=a.y;
      return 'M '+x+' '+(y-5)+' C '+[x+side*42,y-32,x+side*45,y+34,x,y+5].join(' ');
    }
    const dx=b.x-a.x, dy=b.y-a.y, length=Math.hypot(dx,dy);
    const ux=length?dx/length:1, uy=length?dy/length:0;
    // 反向时法向量也反向，同一对模块的两根线从不同插口进出。
    const offset=kind==='relay'?3.5:5;
    const [x1,y1]=socket(a,ux,uy,1,offset), [x2,y2]=socket(b,ux,uy,-1,offset);
    const sag=Math.min(39,Math.hypot(x2-x1,y2-y1)*.11);
    let bow=kind==='relay'?14:5;
    if (kind==='relay' && core) {
      const along=(core.x-a.x)*ux+(core.y-a.y)*uy;
      const across=(core.y-a.y)*ux-(core.x-a.x)*uy;
      const clearance=Math.abs(uy)*core.halfWidth+Math.abs(ux)*core.halfHeight;
      if (along>0 && along<length && Math.abs(across)<clearance+12) {
        bow=(across<0?1:-1)*(clearance+45);
      }
    }
    const cx=(x2-x1)/3, cy=(y2-y1)/3;
    return 'M '+x1+' '+y1+' C '+[x1+cx-uy*bow,y1+cy+sag+ux*bow,x2-cx-uy*bow,y2-cy+sag+ux*bow,x2,y2].join(' ');
  }

  return class OrganFlowView {
    constructor(stage, list, edit) {
      this.stage=stage; this.list=list; this.edit=edit;
      this.nodes=new Map(); this.links=[]; this.positions=new Map();
      try { this.positions=new Map(JSON.parse(localStorage.getItem(layoutKey)||'[]')); } catch {}
      const observer=new ResizeObserver(() => this.layout());
      observer.observe(stage);
      this.field = new PixelField(stage.closest('.page') || stage, pointer => {
        const box = stage.getBoundingClientRect();
        const active = Boolean(pointer && box.width && box.height);
        stage.classList.toggle('og-tracking', active);
        for (const node of this.nodes.values()) {
          const tilt = active && !node.button.classList.contains('dragging')
            ? PixelField.tiltAt(node, pointer.x-box.left, pointer.y-box.top, 13) : '';
          node.face.style.setProperty('--og-tilt', tilt);
        }
      });
    }
    update(cards, connections) {
      this.nodes.clear(); this.links=[];
      const layer=svg('svg',{class:'og-cables','aria-hidden':'true'}), defs=svg('defs');
      layer.append(defs); this.stage.replaceChildren(layer); this.list.replaceChildren();
      for (const kind of ['call','return','relay']) {
        const marker=svg('marker',{id:'og-tip-'+kind,viewBox:'0 0 8 8',refX:7,refY:4,markerWidth:7,markerHeight:7,markerUnits:'userSpaceOnUse',orient:'auto'});
        marker.append(svg('path',{d:'M0 0 L8 4 L0 8 L2 4 Z',fill:'var(--og-'+kind+')'})); defs.append(marker);
      }
      const rows=Math.ceil(cards.length/2);
      this.stage.style.height=Math.max(410,rows*152+88)+'px';
      const core=document.createElement('button'); core.type='button'; core.className='og-node og-core';
      const title=document.createElement('strong');
      title.textContent='CORE'; core.title='核心调度 · 查看流程说明'; core.append(title);
      this.mount(null,core,[.5,.5]);
      cards.forEach(({id,element},i) => {
        const row=Math.floor(i/2);
        const left=(i%2)===(row%2);
        this.mount(id,element,[left?.13:.87,rows===1?.5:.08+row/(rows-1)*.84]);
      });
      connections.forEach((edge,i) => {
        if (!this.nodes.has(edge.source)||!this.nodes.has(edge.target)) return;
        const group=svg('g',{class:'og-edge '+edge.kind});
        const path=svg('path',{class:'og-wire','marker-end':'url(#og-tip-'+edge.kind+')'});
        const pulse=svg('path',{class:'og-pulse',pathLength:100});
        pulse.style.animationDelay=-(i*.47)+'s';
        const description=svg('title'); description.textContent=titleOf(edge.source)+' → '+titleOf(edge.target)+' · '+edge.label;
        group.append(description,path,pulse); layer.append(group); this.links.push({edge,group,path,pulse});
        const item=document.createElement('li'), route=document.createElement('b'), label=document.createElement('span');
        route.textContent=titleOf(edge.source)+' → '+titleOf(edge.target); label.textContent=edge.label;
        item.className=edge.kind; item.append(route,label); this.list.append(item);
      });
      this.layout();
    }
    mount(id, button, home) {
      const face=document.createElement('span');
      face.className='og-tile';
      face.append(...button.childNodes);
      button.append(face);
      const node={id,button,face,home}; this.nodes.set(id,node); this.stage.append(button);
      let gesture=null, suppress=false;
      button.onpointerdown=e => {
        if (e.button!==0||!e.isPrimary) return;
        gesture={id:e.pointerId,x:e.clientX,y:e.clientY,originX:node.x,originY:node.y,moved:false};
        suppress=false; button.setPointerCapture(e.pointerId);
      };
      button.onpointermove=e => {
        if (gesture && gesture.id===e.pointerId) {
          const dx=e.clientX-gesture.x, dy=e.clientY-gesture.y;
          if (!gesture.moved && Math.hypot(dx,dy)<5) return;
          gesture.moved=true; button.classList.add('dragging');
          this.move(node,gesture.originX+dx,gesture.originY+dy);
        }
      };
      const finish=e => {
        if (!gesture||e.pointerId!==gesture.id) return;
        suppress=gesture.moved; gesture=null; button.classList.remove('dragging');
        if (suppress) {
          this.save();
          if (this.field.pointer) this.field.refresh();
        }
      };
      button.onpointerup=finish; button.onpointercancel=finish; button.onlostpointercapture=finish;
      button.onpointerleave=() => {if (!gesture) this.highlight();};
      button.onpointerenter=() => this.highlight(id);
      button.onfocus=() => this.highlight(id); button.onblur=() => this.highlight();
      button.onclick=e => {
        if (suppress && e.detail) {suppress=false;return;}
        if (id!==null) this.edit(id);
        else {const details=this.list.closest('details'); details.open=!details.open; if(details.open) details.querySelector('summary').focus();}
      };
      button.onkeydown=e => {
        const delta={ArrowLeft:[-1,0],ArrowRight:[1,0],ArrowUp:[0,-1],ArrowDown:[0,1]}[e.key];
        if (!delta) return;
        e.preventDefault(); const step=e.shiftKey?24:8;
        this.move(node,node.x+delta[0]*step,node.y+delta[1]*step); this.save();
      };
    }
    highlight(id) {
      for (const {edge,group} of this.links) group.classList.toggle('quiet',id!==undefined && edge.source!==id && edge.target!==id);
    }
    layout() {
      const width=this.stage.clientWidth, height=this.stage.clientHeight;
      if (!width) return;
      for (const node of this.nodes.values()) {
        node.halfWidth=node.button.offsetWidth/2; node.halfHeight=node.button.offsetHeight/2;
        const mx=node.halfWidth+7, my=node.halfHeight+10;
        node.bounds=[mx,my,Math.max(1,width-2*mx),Math.max(1,height-2*my)];
        const saved=this.positions.get(node.id);
        const p=Array.isArray(saved)&&saved.length===2&&saved.every(Number.isFinite)?saved:node.home;
        this.place(node,clamp(p[0],0,1),clamp(p[1],0,1));
      }
      this.draw();
    }
    place(node,u,v) {
      const [left,top,w,h]=node.bounds;
      const x=Math.round(left+u*w-node.halfWidth), y=Math.round(top+v*h-node.halfHeight);
      node.x=x+node.halfWidth; node.y=y+node.halfHeight;
      node.button.style.left=x+'px'; node.button.style.top=y+'px';
    }
    move(node,x,y) {
      const [left,top,w,h]=node.bounds;
      const u=clamp((x-left)/w,0,1), v=clamp((y-top)/h,0,1);
      this.positions.set(node.id,[u,v]); this.place(node,u,v); this.draw();
    }
    draw() {
      for (const {edge,path,pulse} of this.links) {
        const d=cable(this.nodes.get(edge.source),this.nodes.get(edge.target),edge.kind,this.nodes.get(null));
        path.setAttribute('d',d); pulse.setAttribute('d',d);
      }
      if (this.field.pointer) this.field.refresh();
    }
    save() {try {localStorage.setItem(layoutKey,JSON.stringify([...this.positions]));} catch {}}
    reset() {this.positions.clear(); this.save(); this.layout();}
  };
})();
