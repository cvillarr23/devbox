const {test}=require('node:test');
const assert=require('node:assert/strict');
const {arrange}=require('../web/session-cards.js');
const sessions=[{name:'s10',last_activity_at:10,agent_names:['claude']},{name:'s2',last_activity_at:20,agent_names:['codex']},{name:'unknown',last_activity_at:null,agent_names:[]}];
test('newest first, unknown last, and ascending reverses known values',()=>{
  assert.deepEqual(arrange(sessions,'last_activity_at','desc','none')[0][1].map(s=>s.name),['s2','s10','unknown']);
  assert.deepEqual(arrange(sessions,'last_activity_at','asc','none')[0][1].map(s=>s.name),['s10','s2','unknown']);
});
test('natural name order and agent grouping preserve every session',()=>{
  assert.deepEqual(arrange(sessions,'name','asc','none')[0][1].map(s=>s.name),['s2','s10','unknown']);
  const groups=arrange(sessions,'name','asc','agent');
  assert.equal(groups.length,3);
  assert.equal(groups.flatMap(g=>g[1]).length,3);
});
