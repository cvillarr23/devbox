(function(root) {
  const text = (a,b) => String(a).localeCompare(String(b), undefined, {numeric:true, sensitivity:'base'});
  function value(s, key) {
    if (key === 'agent') return (s.agent_names || []).join(', ') || 'No active agent';
    if (key === 'folder') return (s.folders || []).join(', ') || 'Unknown folder';
    if (key === 'repo') return (s.repositories || []).map(r=>r.path).join(', ') || 'No repository detected';
    if (key === 'status') return s.status || 'idle';
    if (key === 'name') return s.name;
    return s[key];
  }
  function arrange(sessions, sort, direction, group) {
    const sorted = [...sessions].sort((a,b)=>{
      const av=value(a,sort), bv=value(b,sort);
      // Unknown timestamps always sort last, in either direction.
      if (av == null || bv == null) return av == null && bv == null ? text(a.name,b.name) : av == null ? 1 : -1;
      const cmp = typeof av === 'number' ? av-bv : text(av,bv);
      return cmp * (direction === 'asc' ? 1 : -1) || text(a.name,b.name);
    });
    const groups = new Map();
    for (const session of sorted) {
      const key = group === 'none' ? '' : value(session,group);
      if (!groups.has(key)) groups.set(key,[]);
      groups.get(key).push(session);
    }
    return [...groups].sort((a,b)=>text(a[0],b[0]));
  }
  root.devboxCards={arrange};
  if(typeof module!=='undefined') module.exports=root.devboxCards;
})(typeof window==='undefined'?globalThis:window);
