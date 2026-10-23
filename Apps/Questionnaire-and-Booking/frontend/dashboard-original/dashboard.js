const $ = id => document.getElementById(id);
let selected = null;
let scenarios = [];
let refreshing = false;
const label = s => (s || '').replaceAll('_',' ');
function node(tag, text, cls) {const el=document.createElement(tag); if(text!==undefined)el.textContent=text;if(cls)el.className=cls;return el;}
async function api(path, body) {
  const response=await fetch('/sandbox/api/'+path,{method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)})});
  const data=await response.json();if(!response.ok)throw new Error(data.error?.message||'Sandbox request failed');return data;
}
function link(text,url){const a=node('a',text,'button');a.href=url;a.target='_blank';a.rel='noopener';return a;}
const STAGES=['enquiried','Questionnaire_sent','questionnaire_completed','viewing_requested','booked','done'];
function stages(done){const reached=STAGES.indexOf(done);return STAGES.map((s,i)=>[s,i<=reached]);}
function nameOf(c){return (c.first_name+' '+c.last_name).trim()||c.email;}
function renderDetail(c){
  const root=$('detail');root.replaceChildren();root.append(node('h2',nameOf(c)),node('p','Contact '+c.id));
  root.append(node('h3','Journey stage'));root.append(node('p',label(c.status||'new')));
  const actions=node('div',undefined,'actions');
  if(c.links.questionnaire)actions.append(link('Open questionnaire ↗',c.links.questionnaire));
  if(c.links.booking)actions.append(link('Open booking form ↗',c.links.booking));
  root.append(actions);
  if(c.viewing_slot_start)root.append(node('p','Viewing '+label(c.status)+' · '+new Date(c.viewing_slot_start).toLocaleString('en-GB',{timeZone:'Europe/London'})));
  root.append(node('h3','Identity'));
  const meta=node('div',undefined,'meta');[['Email',c.email||'Not extracted'],['Phone',c.phone||'—'],['First seen',new Date(c.created_at).toLocaleString()],['Updated',new Date(c.updated_at).toLocaleString()]].forEach(([k,v])=>meta.append(node('strong',k),node('span',v)));root.append(meta);
}
async function refresh(){if(refreshing)return;refreshing=true;try{const {runs}=await api('runs');const contacts=runs;$('connection').textContent='Live · '+new Date().toLocaleTimeString();$('total').textContent=contacts.length;$('completed').textContent=contacts.filter(c=>STAGES.indexOf(c.status)>=STAGES.indexOf('questionnaire_completed')).length;$('viewings').textContent=contacts.filter(c=>!!c.viewing_slot_start).length;$('duplicates').textContent='0';$('runs').replaceChildren();if(!selected&&contacts.length)selected=contacts[0].id;contacts.forEach(c=>{const b=node('button',nameOf(c),'run'+(selected===c.id?' selected':''));b.append(node('span',label(c.status||'new')+' · '+new Date(c.updated_at).toLocaleTimeString()));b.onclick=()=>{selected=c.id;refresh();};$('runs').append(b);});const contact=contacts.find(c=>c.id===selected);if(contact)renderDetail(contact);}catch(e){$('connection').textContent='Unavailable';$('notice').textContent=e.message;}finally{refreshing=false;}}
function updateIndex(){const spec=scenarios.find(s=>s.id===$('scenario').value);$('fixture').max=(spec?.count||1)-1;$('fixture').value=0;}
$('scenario').onchange=updateIndex;
$('generate').onsubmit=async e=>{e.preventDefault();$('send').disabled=true;try{await api('runs',{scenario:$('scenario').value,fixtureIndex:Number($('fixture').value)});selected=null;$('notice').textContent='Enquiry sent through mock SMTP. Waiting for intake…';await refresh();}catch(err){$('notice').textContent=err.message;}finally{$('send').disabled=false;}};
(async()=>{try{const data=await api('scenarios');scenarios=data.scenarios;scenarios.forEach(s=>{const option=node('option',s.label+' ('+s.count+' fixtures)');option.value=s.id;$('scenario').append(option);});updateIndex();await refresh();setInterval(refresh,2500);}catch(e){$('notice').textContent=e.message;}})();
