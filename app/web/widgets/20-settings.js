// Settings: display title (also the browser tab title), description, the
// directory a (re)created session starts in, an accent color, and kill.
// The zellij session name itself never changes - see devbox_store.py.
devbox.registerWidget({
  id: 'settings',
  title: 'Session settings',
  order: 20,
  mount(el, ctx) {
    const COLORS = ['#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#a855f7', '#ec4899', '#14b8a6', '#64748b'];
    el.innerHTML = `
      <label>Title <span class="muted">(shown on the dashboard and browser tab)</span></label>
      <input data-field="title" maxlength="80" placeholder="${ctx.session}">
      <label>Description</label>
      <textarea data-field="description" rows="2" maxlength="500"></textarea>
      <label>Working directory <span class="muted">(used when the session is (re)created)</span></label>
      <input data-field="cwd" placeholder="/workspace">
      <label>Color</label>
      <div class="swatches" style="display:flex;gap:.3rem;flex-wrap:wrap;align-items:center"></div>
      <div class="muted status" style="margin-top:.4rem">&nbsp;</div>
      <div style="margin-top:.8rem;display:flex;justify-content:space-between;align-items:center">
        <span class="muted">Session: ${ctx.session}</span>
        <button class="btn danger" data-act="kill">Kill session</button>
      </div>`;
    const status = el.querySelector('.status');
    const setStatus = (t, isErr) => {
      status.textContent = t || ' ';
      status.style.color = isErr ? 'var(--danger)' : '';
    };

    async function save(patch) {
      try {
        await ctx.setMeta(patch);
        setStatus('Saved');
      } catch (e) {
        setStatus(e.message, true);
      }
    }

    for (const input of el.querySelectorAll('[data-field]')) {
      input.addEventListener('change', () => {
        const field = input.dataset.field;
        const value = input.value.trim();
        if (value !== (ctx.meta[field] || '')) save({[field]: value});
      });
    }

    const swatches = el.querySelector('.swatches');
    const hex = document.createElement('input');
    hex.placeholder = '#rrggbb';
    hex.style.width = '6.5rem';
    const swatchButtons = ['', ...COLORS].map((c) => {
      const b = document.createElement('button');
      b.className = 'btn';
      b.title = c || 'No color';
      b.dataset.color = c;
      b.style.cssText = `width:22px;height:22px;padding:0;border-radius:50%;background:${c || 'transparent'}`;
      if (!c) b.textContent = '×';
      b.onclick = () => save({color: c});
      swatches.appendChild(b);
      return b;
    });
    swatches.appendChild(hex);
    hex.addEventListener('change', () => save({color: hex.value.trim()}));

    function render(m) {
      for (const input of el.querySelectorAll('[data-field]')) {
        if (document.activeElement !== input) input.value = m[input.dataset.field] || '';
      }
      if (document.activeElement !== hex) hex.value = m.color || '';
      for (const b of swatchButtons) {
        b.style.outline = b.dataset.color === (m.color || '') ? '2px solid var(--fg)' : 'none';
      }
    }
    ctx.onMetaChange(render);
    render(ctx.meta);

    el.querySelector('[data-act="kill"]').onclick = async () => {
      if (!confirm('Kill session "' + ctx.session + '"? This ends it for good (notes are archived).')) return;
      await ctx.api('', {method: 'DELETE'});
      location.href = '/';
    };
  },
});
