// Agents: Claude Code / Codex running in this session's panes, as reported
// by their hooks (devbox-status). The page itself drives the favicon, tab
// title and notifications; this widget lists each pane and owns the
// "enable notifications" click browsers require.
devbox.registerWidget({
  id: 'agents',
  title: 'Agents',
  order: 5,
  mount(el, ctx) {
    const COLORS = {working: '#60a5fa', waiting: '#f59e0b', done: '#22c55e', idle: 'var(--muted)'};
    el.innerHTML = `
      <div class="rows"></div>
      <div class="notify muted" style="margin-top:.5rem"></div>`;
    const rows = el.querySelector('.rows');
    const notify = el.querySelector('.notify');

    const ago = (since) => {
      const s = Math.max(0, Math.round(Date.now() / 1000 - since));
      if (s < 60) return s + 's';
      if (s < 3600) return Math.floor(s / 60) + 'm';
      return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
    };

    function render(st) {
      rows.innerHTML = '';
      if (!st.agents.length) {
        rows.innerHTML = '<div class="muted">No agents reporting. Claude Code and Codex show up here once started (or restarted) in this session.</div>';
        return;
      }
      for (const a of st.agents) {
        const state = a.state === 'done' && a.acked ? 'idle' : a.state;
        const row = document.createElement('div');
        row.style.cssText = 'display:flex;align-items:center;gap:.45rem;padding:.2rem 0';
        const dot = document.createElement('span');
        dot.style.cssText = `width:.6rem;height:.6rem;border-radius:50%;flex:none;background:${COLORS[state] || COLORS.idle}`;
        const label = document.createElement('span');
        label.style.flex = '1';
        label.textContent = `${a.agent} · ${a.state === 'done' && a.acked ? 'done (seen)' : a.state === 'unavailable' ? 'status unavailable' : a.state}`;
        const meta = document.createElement('span');
        meta.className = 'muted';
        meta.textContent = `pane ${a.pane} · ${ago(a.since)}`;
        row.append(dot, label, meta);
        rows.appendChild(row);
      }
    }

    function renderNotify() {
      notify.innerHTML = '';
      if (!('Notification' in window)) {
        notify.textContent = 'This browser has no notifications.';
        return;
      }
      if (Notification.permission === 'denied') {
        notify.textContent = 'Notifications are blocked for this site in browser settings.';
        return;
      }
      const btn = document.createElement('button');
      btn.className = 'btn';
      if (Notification.permission === 'default') {
        btn.textContent = 'Enable notifications';
        btn.onclick = async () => {
          await Notification.requestPermission();
          ctx.prefs.set('notify', true);
          renderNotify();
          ctx.focusTerminal();
        };
      } else {
        const on = ctx.prefs.get('notify', true);
        btn.textContent = on ? 'Notifications on' : 'Notifications off';
        btn.title = 'Notify when an agent needs input or finishes while this tab is in the background';
        btn.onclick = () => { ctx.prefs.set('notify', !on); renderNotify(); ctx.focusTerminal(); };
      }
      notify.appendChild(btn);
    }

    ctx.onStatusChange(render);
    render(ctx.status);
    renderNotify();
  },
});
