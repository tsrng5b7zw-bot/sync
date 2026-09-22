# Reading fplform.com in the browser (projections are not in the repo)

fplform's projections are copyrighted, so the collector does not scrape them. Read them live from
the page with the built-in browser pane (preferred: it returns large outputs intact) or Claude in Chrome
(truncates output at ~1 KB — page the output in slices with `.slice(a, b)`).

Steps: navigate the pane to the URL, wait for the table, run the snippet with the javascript tool,
paste the returned text into a file, then run `python tools/build_dataset.py`.

## 1. Predicted points → `proj_raw.txt`

URL: https://fplform.com/fpl-predicted-points

```js
const dt = jQuery('#players').DataTable();
const H = [...document.querySelector('#players thead tr').cells].map(c => c.innerText.replace(/\s+/g,' ').trim());
const ix = (re) => H.findIndex(h => re.test(h));
const gwCols = H.map((h,i)=>[h,i]).filter(([h])=>/^Pts GW \d+$/.test(h));
const cols = {name:1, team:ix(/^Team$/), pos:ix(/^Pos$/), cost:ix(/^Cost$/), merit:ix(/^Merit$/), form:ix(/^Form$/), prob:ix(/Prob\. of Appear/),
  nextn:ix(/^Pts Next \d+$/), ros:ix(/^Pts Rest Of Season$/), sofar:ix(/^Points So Far$/), chance:ix(/^Official Chance$/),
  avail:ix(/^Official Availability$/), sel:ix(/^Selected By %$/), news:ix(/^News$/)};
const strip = x => (typeof x==='string' ? x.replace(/<[^>]+>/g,' ').replace(/\s+/g,' ').trim() : String(x));
const head = ['name','team','pos','cost','merit','form','prob', ...gwCols.map(([h])=>h.replace('Pts GW ','gw')), 'nextn','ros','sofar','chance','avail','sel','news'];
const rows = [head.join('|')];
dt.rows().every(function(){ const d=this.data().map(strip); if(parseFloat(d[cols.nextn])>=5){
  rows.push([d[1],d[cols.team],d[cols.pos],d[cols.cost],d[cols.merit],d[cols.form],d[cols.prob], ...gwCols.map(([,i])=>d[i]), d[cols.nextn],d[cols.ros],d[cols.sofar],d[cols.chance],d[cols.avail],d[cols.sel],d[cols.news].slice(0,80)].join('|')); }});
rows.join('\n')
```

The threshold `>= 5` keeps ~500 rows (everyone who matters). Save the output verbatim as `proj_raw.txt`.

## 2. Player data (FPL API mirror with per-player rows) → optional

URL: https://fplform.com/fpl-player-data — not needed when the repo is up to date (players.csv has the same
fields). Use only if the repo is stale:

```js
const t=document.getElementById('playerdata'); const rows=[];
for (const r of t.tBodies[0].rows){ const c=[...r.cells].map(x=>x.innerText.trim().replace(/\s+/g,' ')); if(c.length<44) continue;
  if((parseFloat(c[4])||0)>=6 || (parseFloat(c[23])||0)>=200) rows.push([c[0],c[1],c[2],c[3],c[4],c[23],c[24],c[25],c[26],c[28],c[37],c[34],c[12],c[15],c[16],c[18],c[11],c[43]].join('|')); }
'name|pos|team|price|pts|min|G|A|defcon|CS|bonus|YC|pen|chance|status|sel|bps|id\n'+rows.join('\n')
```

## 3. Price-change check (FPL API, exact)

URL: https://fantasy.premierleague.com/api/bootstrap-static/

```js
const d = await fetch('/api/bootstrap-static/', {cache:'no-store'}).then(r=>r.json());
const T = Object.fromEntries(d.teams.map(t=>[t.id,t.short_name]));
d.elements.filter(e => e.cost_change_event !== 0).map(e => `${e.web_name}(${T[e.team]})${e.cost_change_event>0?'+':'-'}${(e.now_cost/10).toFixed(1)}`).join(' | ')
```

Price changes land at midnight UK time (new for 2026/27; before, it was 1.30 am GMT / 2.30 am BST). That is 6 pm Chicago for most of the season, but 7 pm while only one country has changed its clocks: 25–31 Oct 2026 and 14–27 Mar 2027.
