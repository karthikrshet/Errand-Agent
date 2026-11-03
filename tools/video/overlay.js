// Injected into every recorded page: animated cursor, click ripple, captions, scene chip,
// row highlighting and smooth zoom. Driven from tools/video/make_video.py via page.evaluate.
(() => {
  if (window.__overlayReady) return;
  window.__overlayReady = true;
  const css = `
    #__cur{position:fixed;left:-60px;top:-60px;width:30px;height:30px;z-index:2147483647;pointer-events:none;
      transition:left .75s cubic-bezier(.22,.8,.3,1),top .75s cubic-bezier(.22,.8,.3,1);
      filter:drop-shadow(0 3px 6px rgba(0,0,0,.55))}
    #__rip{position:fixed;width:54px;height:54px;border-radius:50%;border:3px solid #ffd166;z-index:2147483646;
      pointer-events:none;opacity:0;transform:translate(-50%,-50%) scale(.3)}
    #__rip.go{animation:__rip .55s ease-out}
    @keyframes __rip{0%{opacity:.95;transform:translate(-50%,-50%) scale(.3)}100%{opacity:0;transform:translate(-50%,-50%) scale(1.5)}}
    #__cap{position:fixed;left:50%;bottom:26px;transform:translate(-50%,8px);max-width:1040px;padding:12px 24px;
      border-radius:14px;background:rgba(7,9,15,.86);color:#f5f7fb;font:600 21px/1.4 'Segoe UI',system-ui,sans-serif;
      text-align:center;z-index:2147483645;opacity:0;transition:opacity .35s,transform .35s;
      box-shadow:0 10px 34px rgba(0,0,0,.5);border:1px solid rgba(255,255,255,.14);letter-spacing:.1px}
    #__cap.on{opacity:1;transform:translate(-50%,0)}
    #__chip{position:fixed;right:22px;top:58px;z-index:2147483645;padding:6px 14px;border-radius:999px;
      font:700 12px/1 'Segoe UI',system-ui,sans-serif;letter-spacing:.14em;text-transform:uppercase;
      color:#0b0f19;background:#ffd166;opacity:0;transition:opacity .4s}
    #__chip.on{opacity:1}
    .__hl{outline:2px solid #ffd166 !important;background:rgba(255,209,102,.20) !important;border-radius:5px;
      transition:background .3s,outline .3s}
    .__zoom{transition:transform 1.1s cubic-bezier(.22,.8,.3,1);transform-origin:center center}
  `;
  const style = document.createElement('style');
  style.textContent = css;
  const cursor = document.createElement('div');
  cursor.id = '__cur';
  cursor.innerHTML = `<svg viewBox="0 0 24 24" width="30" height="30"><path d="M4 2 L4 20 L9 15.5 L12.5 22.5 L15.5 21 L12 14.2 L19 14.2 Z"
    fill="#ffffff" stroke="#111" stroke-width="1.4" stroke-linejoin="round"/></svg>`;
  const ripple = document.createElement('div');
  ripple.id = '__rip';
  const cap = document.createElement('div');
  cap.id = '__cap';
  const chip = document.createElement('div');
  chip.id = '__chip';
  const mount = () => {
    document.head.appendChild(style);
    document.body.append(cursor, ripple, cap, chip);
    // In the real app the chat input sits at the bottom: lift the caption above it.
    // In the real app the newest chat and trace rows are at the bottom: caption on top instead.
    if (location.protocol.startsWith('http')) {
      cap.style.bottom = 'auto'; cap.style.top = '60px'; cap.style.maxWidth = '760px';
      cap.style.fontSize = '19px';
    }
  };
  if (document.body) mount(); else document.addEventListener('DOMContentLoaded', mount);

  window.__cursor = (x, y) => { cursor.style.left = x + 'px'; cursor.style.top = y + 'px'; };
  window.__ripple = (x, y) => {
    ripple.style.left = x + 'px'; ripple.style.top = y + 'px';
    ripple.classList.remove('go'); void ripple.offsetWidth; ripple.classList.add('go');
  };
  window.__caption = (text) => {
    if (!text) { cap.classList.remove('on'); return; }
    cap.classList.remove('on');
    setTimeout(() => { cap.textContent = text; cap.classList.add('on'); }, 120);
  };
  window.__chip = (text) => {
    chip.textContent = text || '';
    chip.classList.toggle('on', !!text);
  };
  window.__highlight = (needle, rootSel) => {
    const root = document.querySelector(rootSel || 'body');
    const rows = [...root.querySelectorAll('.ev, .msg')].filter(r => r.textContent.includes(needle));
    rows.forEach(r => r.classList.add('__hl'));
    if (rows.length) rows[rows.length - 1].scrollIntoView({behavior: 'smooth', block: 'center'});
    return rows.length;
  };
  window.__unhighlight = () => document.querySelectorAll('.__hl').forEach(r => r.classList.remove('__hl'));
  window.__zoom = (sel, scale) => {
    const el = document.querySelector(sel);
    if (!el) return;
    el.classList.add('__zoom');
    el.style.transform = scale && scale !== 1 ? `scale(${scale})` : '';
  };
})();
