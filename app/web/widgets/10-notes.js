// Notes: a per-session notepad persisted at
// /var/lib/dev-workspace/sessions/<name>/notes.md. Agents in the session
// write the same file with the `devbox-notes` CLI, so the widget polls for
// outside changes and never silently overwrites them.
devbox.registerWidget({
  id: 'notes',
  title: 'Notes',
  order: 10,
  async mount(el, ctx) {
    el.innerHTML = `
      <div class="banner" hidden>
        Notes changed outside this page (probably an agent).
        <div class="actions">
          <button class="btn" data-act="theirs">Load theirs</button>
          <button class="btn" data-act="mine">Keep mine</button>
        </div>
      </div>
      <textarea rows="16" spellcheck="false"
        placeholder="Notes for this session. Agents can add to them with: devbox-notes append &quot;...&quot;"></textarea>
      <div class="muted status">&nbsp;</div>`;
    const ta = el.querySelector('textarea');
    const banner = el.querySelector('.banner');
    const status = el.querySelector('.status');
    let version = null;   // version of the file the textarea is based on
    let dirty = false;    // textarea has edits not yet saved
    let saving = false;
    let remote = null;    // {content, version} waiting in the conflict banner
    let timer = null;

    const setStatus = (t) => { status.textContent = t || ' '; };
    const setText = (content, v) => {
      ta.value = content;
      version = v;
      dirty = false;
    };
    const showConflict = (r) => {
      remote = r;
      banner.hidden = false;
      setStatus('Not saved - resolve the change above.');
    };

    async function save() {
      if (saving || !banner.hidden) return;
      saving = true;
      const content = ta.value;
      try {
        const r = await ctx.api('/notes', {method: 'PUT', body: {content, base_version: version}});
        if (r.ok) {
          version = r.data.version;
          if (ta.value === content) {
            dirty = false;
            setStatus('Saved ' + new Date().toLocaleTimeString());
          }
        } else if (r.status === 409) {
          showConflict(r.data);
        } else {
          setStatus('Save failed: ' + ((r.data && r.data.error) || r.status));
        }
      } finally {
        saving = false;
      }
      if (dirty && banner.hidden) schedule();
    }

    function schedule() {
      clearTimeout(timer);
      timer = setTimeout(save, 800);
    }

    async function poll() {
      if (document.hidden || saving || !banner.hidden) return;
      const r = await ctx.api('/notes');
      if (!r.ok || r.data.version === version) return;
      if (dirty) showConflict(r.data);
      else setText(r.data.content, r.data.version);
    }

    ta.addEventListener('input', () => {
      dirty = true;
      setStatus('Editing...');
      schedule();
    });
    ta.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') ctx.focusTerminal();
    });
    banner.addEventListener('click', (e) => {
      const act = e.target.dataset.act;
      if (!act || !remote) return;
      banner.hidden = true;
      if (act === 'theirs') {
        setText(remote.content, remote.version);
        setStatus('Loaded the newer version.');
      } else {
        // Overwrite on top of the version we now know about.
        version = remote.version;
        dirty = true;
        save();
      }
      remote = null;
    });

    const r = await ctx.api('/notes');
    if (!r.ok) throw new Error('could not load notes (' + r.status + ')');
    setText(r.data.content, r.data.version);
    setInterval(poll, 3000);
  },
});
