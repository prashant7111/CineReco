document.addEventListener('DOMContentLoaded', () => {
  const loader = document.getElementById('page-loader');
  const showLoader = () => { if (loader) { loader.classList.add('show'); loader.setAttribute('aria-hidden','false'); } };
  const hideLoader = () => { if (loader) { loader.classList.remove('show'); loader.setAttribute('aria-hidden','true'); } };
  window.addEventListener('pageshow', hideLoader);
  window.addEventListener('load', hideLoader);
  setTimeout(hideLoader, 1800);

  document.querySelectorAll('.toast').forEach(x => setTimeout(() => x.remove(), 3500));

  // Profile dropdown
  const profileMenu = document.querySelector('.profile-menu');
  const profileTrigger = document.querySelector('.profile-trigger');
  if (profileMenu && profileTrigger) {
    profileTrigger.addEventListener('click', e => { e.stopPropagation(); const open = profileMenu.classList.toggle('open'); profileTrigger.setAttribute('aria-expanded', open ? 'true' : 'false'); });
    document.addEventListener('click', () => { profileMenu.classList.remove('open'); profileTrigger.setAttribute('aria-expanded','false'); });
  }

  // Smooth scroll controls
  document.querySelectorAll('[data-scroll]').forEach(b => b.addEventListener('click', () => window.scrollTo({top: b.dataset.scroll === 'top' ? 0 : document.documentElement.scrollHeight, behavior: 'smooth'})));

  // Filter drawer
  const filterToggle = document.getElementById('filter-toggle');
  const filterPanel = document.getElementById('filter-panel');
  if (filterToggle && filterPanel) filterToggle.addEventListener('click', () => filterPanel.classList.toggle('open'));

  // Fast toggle actions: favorite, wishlist, watched. No page reload, no long loader.
  document.querySelectorAll('[data-toggle-form]').forEach(form => form.addEventListener('submit', async e => {
    e.preventDefault();
    const btn = form.querySelector('button'); if (!btn || btn.disabled) return;
    const original = btn.textContent; btn.disabled = true; btn.classList.add('is-loading');
    try {
      const r = await fetch(form.action, {method:'POST', headers:{'X-Requested-With':'XMLHttpRequest'}});
      const data = await r.json();
      if (!r.ok || !data.ok) throw new Error();
      btn.textContent = data.active ? form.dataset.on : form.dataset.off;
      btn.classList.toggle('state-on', !!data.active);
      toast(data.label || (data.active ? 'Saved' : 'Removed'));
    } catch(err) { btn.textContent = original; toast('Could not update right now. Try again.', 'error'); }
    finally { btn.disabled = false; btn.classList.remove('is-loading'); }
  }));

  // Movie rails: fast horizontal navigation with edge-aware arrows.
  const refreshRail = (rail) => {
    const wrap = rail.closest('.rail-wrap'); if (!wrap) return;
    const left = wrap.querySelector('[data-rail-dir=left]'); const right = wrap.querySelector('[data-rail-dir=right]');
    const max = Math.max(0, rail.scrollWidth - rail.clientWidth - 2);
    if (left) left.disabled = rail.scrollLeft <= 2;
    if (right) right.disabled = rail.scrollLeft >= max;
  };
  document.querySelectorAll('.rail').forEach(rail => {
    refreshRail(rail);
    rail.addEventListener('scroll', () => refreshRail(rail), {passive:true});
  });
  document.querySelectorAll('[data-rail-dir]').forEach(btn => btn.addEventListener('click', () => {
    const wrap = btn.closest('.rail-wrap');
    const rail = wrap && wrap.querySelector('.rail');
    if (!rail || btn.disabled) return;
    const amount = Math.max(420, Math.floor(rail.clientWidth * 0.78));
    rail.scrollBy({left: btn.dataset.railDir === 'left' ? -amount : amount, behavior:'smooth'});
    setTimeout(() => refreshRail(rail), 320);
  }));
  window.addEventListener('resize', () => document.querySelectorAll('.rail').forEach(refreshRail));

  // Ratings are saved in the background so a star click never reloads the movie page.
  document.querySelectorAll('.rating-form').forEach(form => form.addEventListener('submit', async e => {
    e.preventDefault();
    const btn = e.submitter || form.querySelector('button[name="rating"]');
    if (!btn || btn.disabled) return;
    const buttons = [...form.querySelectorAll('.star-btn')];
    buttons.forEach(x => x.disabled = true);
    btn.classList.add('is-loading');
    try {
      const body = new URLSearchParams(); body.set('rating', btn.value);
      const r = await fetch(form.action, {method:'POST', headers:{'X-Requested-With':'XMLHttpRequest','Content-Type':'application/x-www-form-urlencoded'}, body});
      const d = await r.json();
      if (!r.ok || !d.ok) throw new Error();
      buttons.forEach(x => x.classList.toggle('active', Number(x.value) <= Number(d.rating)));
      const panel = form.closest('.rating-panel');
      const strong = panel && panel.querySelector('div > strong');
      if (strong) strong.textContent = `${Number(d.rating).toFixed(1)} / 5`;
      let remove = panel && panel.querySelector('.remove-rating');
      if (!remove) {
        const rf = document.createElement('form'); rf.method='post'; rf.action=form.action.replace(/\/rate$/, '/rate/remove'); rf.setAttribute('data-action-form','');
        rf.innerHTML='<button class="remove-rating" type="submit">Remove rating</button>';
        form.after(rf); remove = rf.querySelector('.remove-rating');
        bindRemoveRating(rf);
      }
      toast(d.label || 'Rating saved');
    } catch(err) { toast('Could not save your rating. Try again.', 'error'); }
    finally { buttons.forEach(x => x.disabled=false); btn.classList.remove('is-loading'); }
  }));

  function bindRemoveRating(form){
    form.addEventListener('submit', async e => {
      e.preventDefault(); const btn=form.querySelector('button'); if(!btn || btn.disabled)return; btn.disabled=true;
      try { const r=await fetch(form.action,{method:'POST',headers:{'X-Requested-With':'XMLHttpRequest'}}); const d=await r.json(); if(!r.ok||!d.ok)throw new Error();
        document.querySelectorAll('.star-btn').forEach(x=>x.classList.remove('active'));
        const panel=form.closest('.rating-panel'); const strong=panel&&panel.querySelector('div > strong'); if(strong)strong.textContent='Rate this film'; form.remove(); toast('Rating removed');
      } catch(err){btn.disabled=false;toast('Could not remove the rating.','error');}
    });
  }
  document.querySelectorAll('form[action$="/rate/remove"]').forEach(bindRemoveRating);

  // Navigation uses a tiny top progress line rather than a blocking full-page overlay.
  document.querySelectorAll('a[href]').forEach(a => a.addEventListener('click', e => {
    const href=a.getAttribute('href')||'';
    if(!e.defaultPrevented && href && !href.startsWith('#') && !href.startsWith('javascript:') && !a.target && !a.closest('.suggestions')) showLoader();
  }));

  // Warm movie-detail pages before a click. This removes the perceptual 3–4s wait
  // on repeat/local navigation when the browser can reuse the prefetched document.
  const prefetched = new Set();
  const canPrefetch = !navigator.connection || !navigator.connection.saveData;
  document.querySelectorAll('[data-prefetch=true]').forEach(a => {
    if (!canPrefetch) return;
    let timer=null;
    const warm=()=>{
      const href=a.href; if(!href || prefetched.has(href)) return;
      prefetched.add(href);
      const link=document.createElement('link'); link.rel='prefetch'; link.href=href; link.as='document';
      document.head.appendChild(link);
    };
    a.addEventListener('mouseenter',()=>{ clearTimeout(timer); timer=setTimeout(warm,120); },{passive:true});
    a.addEventListener('mouseleave',()=>clearTimeout(timer),{passive:true});
    a.addEventListener('focus',warm,{passive:true});
  });
  document.querySelectorAll('form:not([data-toggle-form]):not(.rating-form):not([action$="/rate/remove"])').forEach(f => f.addEventListener('submit', () => {
    const b=f.querySelector('button[type="submit"]'); if(b&&!b.disabled){b.disabled=true;b.classList.add('is-loading');if(!b.classList.contains('star-btn'))b.textContent='Working…';} showLoader();
  }));

  // Live movie suggestions.
  const input=document.getElementById('movie-search'); const box=document.getElementById('suggestions'); let timer;
  if(input&&box){
    const close=()=>{box.classList.remove('open');box.innerHTML='';};
    const render=items=>{box.innerHTML=items.map(m=>`<a class="suggestion" href="/movie/${m.movieId}"><span><b>${escapeHtml(m.title)}</b><small>${escapeHtml((m.genres||[]).join(' · '))}</small></span><strong>★ ${Number(m.rating).toFixed(1)}</strong></a>`).join('');box.classList.toggle('open',items.length>0);};
    input.addEventListener('input',()=>{clearTimeout(timer);const q=input.value.trim();if(q.length<2){close();return;}timer=setTimeout(async()=>{try{const r=await fetch(`/api/suggest?q=${encodeURIComponent(q)}`);if(!r.ok)throw new Error();render(await r.json());}catch(e){box.innerHTML='<div class="suggestion-error">Suggestions unavailable — press Search to continue.</div>';box.classList.add('open');}},160);});
    input.addEventListener('focus',()=>{if(input.value.trim().length>=2)input.dispatchEvent(new Event('input'));});
    document.addEventListener('click',e=>{if(!e.target.closest('.search-wrap'))close();});input.addEventListener('keydown',e=>{if(e.key==='Escape')close();});
  }
  function toast(msg,kind='success'){const wrap=document.querySelector('.toasts')||(()=>{const x=document.createElement('div');x.className='toasts';document.body.appendChild(x);return x;})();const x=document.createElement('div');x.className=`toast ${kind}`;x.textContent=msg;wrap.appendChild(x);setTimeout(()=>x.remove(),3000);}
  function escapeHtml(v){return String(v).replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));}
});
