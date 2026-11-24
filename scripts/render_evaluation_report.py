"""Render an evaluate callback (schema evaluate.v2.0) as a standalone HTML report.

    python scripts/render_evaluation_report.py callback.json report.html

The page reads only the public callback JSON and adds no medical judgement of
its own. It lays the callback out as a formal written assessment:

* organised by health domain, not by input: every problem is shown once, with
  its results, assessment, risk and management together, and the gut-microbiome
  features related to a domain sit inside that domain;
* itemised text ("小标题：正文" paragraphs) is set as labelled rows, results are
  drawn against their reference ranges, and tables carry what tables carry
  best (priorities, follow-up schedule, abnormal results);
* impersonal wording, numbered tables and figures, A4 print layout.

It is a reference rendering, never an API artifact.
"""

from __future__ import annotations

import html
import json
import math
import re
import sys
from pathlib import Path

_CSS = """
:root{
  --page:#eceeed;--paper:#fff;--ink:#1b1f1d;--ink-2:#434845;--muted:#676c68;--line:#d5d9d6;--grid:#e7e9e7;--wash:#f5f6f5;
  --accent:#0d5f52;--accent-wash:#e8f1ee;
  --good:#0ca30c;--good-ink:#1c5c1c;--good-wash:#e6f3e4;
  --warn:#fab219;--warn-ink:#6a4500;--warn-wash:#fdf2d6;
  --serious:#ec835a;--serious-ink:#8a3413;--serious-wash:#fbe7dc;
  --critical:#d03b3b;--critical-ink:#9a2121;--critical-wash:#f9e1e0;
  --neutral-ink:#3f423f;--neutral-wash:#eceeec;--band:#cfe6d8;--low:#2a78d6;--high:#eb6834;--mid:#c9cdca;
  --sans:"PingFang SC","Hiragino Sans GB","Noto Sans CJK SC","Source Han Sans SC","Microsoft YaHei",system-ui,-apple-system,"Segoe UI",sans-serif;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth;scroll-padding-top:72px}
body{margin:0;background:var(--page);color:var(--ink);font:15.5px/1.75 var(--sans);-webkit-text-size-adjust:100%}
::selection{background:#cfe3dd}
a{color:var(--accent);text-underline-offset:3px}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:3px}
.shell{display:grid;grid-template-columns:208px minmax(0,860px);gap:32px;justify-content:center;padding:32px 24px 72px}
nav.toc{position:sticky;top:28px;align-self:start;font-size:14px;line-height:1.55}
nav.toc ol{list-style:none;margin:0;padding:0}
nav.toc a{display:block;padding:6px 10px;border-left:2px solid transparent;color:var(--ink-2);text-decoration:none}
nav.toc a:hover{color:var(--accent)}
nav.toc a.active{border-left-color:var(--accent);color:var(--accent);font-weight:600}
nav.toc .who{padding:0 10px 12px;margin-bottom:10px;border-bottom:1px solid var(--line);color:var(--muted);font-size:13px}
nav.toc .who b{display:block;color:var(--ink);font-size:15.5px}
main{min-width:0;background:var(--paper);border:1px solid var(--line);padding:56px 64px 64px}
/* cover */
header.cover{padding:0 0 28px;margin-bottom:8px;border-bottom:3px double var(--ink)}
header.cover .kind{font-size:13px;letter-spacing:.3em;color:var(--muted);margin:0 0 10px}
header.cover h1{font-size:32px;line-height:1.25;margin:0 0 22px;font-weight:650;letter-spacing:.02em}
dl.idcard{display:grid;grid-template-columns:repeat(4,auto minmax(0,1fr));gap:6px 12px;margin:0;font-size:14.5px}
dl.idcard dt{color:var(--muted);white-space:nowrap}
dl.idcard dd{margin:0 16px 0 0;font-weight:600}
dl.idcard dd.wide{grid-column:2/-1}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:0;margin:18px 0 6px;border-top:1.5px solid var(--ink);border-bottom:1.5px solid var(--ink)}
.stats div{padding:12px 14px;border-left:1px solid var(--grid)}
.stats div:first-child{border-left:none;padding-left:0}
.stats b{display:block;font-size:26px;line-height:1.2;font-weight:650;font-variant-numeric:tabular-nums}
.stats b small{font-size:13px;font-weight:500;color:var(--muted);margin-left:3px}
.stats span{font-size:13px;color:var(--ink-2)}
nav.contents{margin:18px 0 0;font-size:14px;line-height:1.7}
nav.contents p{font-weight:650;margin:0 0 4px;letter-spacing:.2em}
nav.contents ol{list-style:none;margin:0;padding:0}
nav.contents>ol{columns:2;column-gap:36px}
nav.contents li{break-inside:avoid}
nav.contents li li{padding-left:1.2em;color:var(--ink-2);font-size:13.5px}
nav.contents a{color:inherit;text-decoration:none}nav.contents a:hover{color:var(--accent)}
nav.toc ol ol{margin:0 0 4px 10px;font-size:13px}
nav.toc ol ol a{padding:3px 10px}
table.tbl td.notes p{margin:0 0 4px}table.tbl td.notes p:last-child{margin:0}
table.tbl td ul.facts{margin:0;font-size:13.5px}
/* headings */
section.chapter{padding-top:36px}
h2{font-size:23px;line-height:1.35;margin:0 0 18px;padding-bottom:10px;border-bottom:1.5px solid var(--ink);font-weight:650}
h2 .no{color:var(--accent);margin-right:.35em}
h3{font-size:18.5px;line-height:1.45;margin:34px 0 12px;font-weight:650}
h3.domain{display:flex;flex-wrap:wrap;align-items:center;gap:6px 14px;padding:10px 14px;background:var(--wash);border-top:2px solid var(--accent);margin-top:40px}
h3.domain .score{margin-left:auto;font-variant-numeric:tabular-nums;font-size:22px}
h3.domain .score small{font-size:13px;font-weight:500;color:var(--muted);margin-left:2px}
h3.domain .theme{flex-basis:100%;font-size:14px;font-weight:500;color:var(--ink-2)}
h4{font-size:16.5px;line-height:1.5;margin:0;font-weight:650}
h5,.blk{font-size:14px;line-height:1.5;margin:18px 0 4px;font-weight:650;color:var(--accent)}
p{margin:0 0 10px}
.lead{font-size:16.5px;line-height:1.85;margin:0 0 14px;padding:14px 18px;background:var(--accent-wash);border-radius:4px}
.gist{color:var(--ink-2);margin:8px 0 0}
.muted{color:var(--muted)}.small{font-size:13.5px;line-height:1.7}
.cite{font-size:11.5px;vertical-align:super;line-height:0;margin-left:1px;white-space:nowrap}
.cite a{text-decoration:none;padding:0 1px}
.cap{font-size:13.5px;color:var(--ink-2);margin:20px 0 8px;font-weight:600}
.cap.fig{margin:8px 0 20px;text-align:center}
.note{font-size:13px;color:var(--muted);margin:6px 0 14px}
.chip{display:inline-flex;align-items:center;gap:4px;padding:0 8px 0 6px;border-radius:3px;font-size:12.5px;line-height:1.8;white-space:nowrap;font-weight:500;background:var(--neutral-wash);color:var(--neutral-ink);vertical-align:middle}
.chip svg{width:11px;height:11px;flex:none}
.chip.good{background:var(--good-wash);color:var(--good-ink)}.chip.warn{background:var(--warn-wash);color:var(--warn-ink)}
.chip.serious{background:var(--serious-wash);color:var(--serious-ink)}.chip.critical{background:var(--critical-wash);color:var(--critical-ink)}
.when{display:inline-block;font-size:12.5px;line-height:1.8;padding:0 8px;border:1px solid var(--accent);color:var(--accent);border-radius:3px;white-space:nowrap}
/* itemised text */
dl.kv{display:grid;grid-template-columns:7.4em minmax(0,1fr);margin:6px 0 4px}
dl.kv dt{font-weight:600;color:var(--ink-2);padding:8px 14px 8px 0;border-top:1px solid var(--grid);line-height:1.6}
dl.kv dd{margin:0;padding:8px 0;border-top:1px solid var(--grid)}
dl.kv dt:first-of-type,dl.kv dt:first-of-type+dd{border-top:none}
ul.pts{margin:6px 0 8px;padding-left:1.2em}
ul.pts li{margin-bottom:4px}
/* tables */
table.tbl{width:100%;border-collapse:collapse;font-size:14px;line-height:1.6;margin:4px 0 10px;border-top:1.5px solid var(--ink);border-bottom:1.5px solid var(--ink)}
table.tbl th{text-align:left;font-weight:600;padding:8px 10px 8px 0;border-bottom:1px solid var(--ink);white-space:nowrap;font-size:13px}
table.tbl td{padding:9px 10px 9px 0;border-bottom:1px solid var(--grid);vertical-align:top}
table.tbl tr:last-child td{border-bottom:none}
table.tbl td.n{width:2.2em;color:var(--muted);font-variant-numeric:tabular-nums}
table.tbl td.num{font-variant-numeric:tabular-nums;white-space:nowrap}
table.tbl td.nw{white-space:nowrap}
table.tbl td b{font-weight:600}
table.tbl a{color:var(--ink);text-decoration-color:var(--line)}
/* scores */
.scores{display:grid;grid-template-columns:minmax(0,330px) minmax(0,1fr);gap:8px 24px;align-items:center;margin:6px 0}
.scores>svg{width:100%;height:auto;display:block}
.meters{display:grid;gap:9px}
.meter{display:grid;grid-template-columns:10em minmax(60px,1fr) 2.2em 5.4em;align-items:center;gap:10px;font-size:14px}
.meter a{color:var(--ink);text-decoration:none}.meter a:hover{color:var(--accent)}
.meter .bar{height:8px;border-radius:4px;background:var(--grid);position:relative}
.meter .bar i{position:absolute;inset:0 auto 0 0;border-radius:4px}
.meter .bar s{position:absolute;top:-3px;bottom:-3px;width:1px;background:var(--muted);opacity:.55}
.meter .v{font-variant-numeric:tabular-nums;font-weight:650;text-align:right}
/* range charts */
.ranges{margin:6px 0 4px}
.rrow{display:grid;grid-template-columns:minmax(7em,12.5em) minmax(110px,1fr) 11.5em;align-items:center;gap:14px;padding:7px 0;border-bottom:1px solid var(--grid);font-size:14.5px}
.ranges>.rrow:last-child,.taxon:last-child{border-bottom:none}
.rrow .nm{line-height:1.45}.rrow .nm small{display:block;color:var(--muted);font-size:12.5px}
.rrow .val{text-align:right;font-variant-numeric:tabular-nums;line-height:1.45}
.rrow .val b{font-weight:650}.rrow .val small{display:block;color:var(--muted);font-size:12.5px}
.rrow .val .ratio{color:var(--serious-ink)}
.track{position:relative;height:22px}
.track:before{content:"";position:absolute;left:0;right:0;top:10px;height:2px;background:var(--grid);border-radius:2px}
.track .band{position:absolute;top:6px;height:10px;background:var(--band);border:1px solid #6fa688;border-radius:5px}
.track .dot{position:absolute;top:4px;width:14px;height:14px;margin-left:-7px;border-radius:50%;border:2px solid var(--paper);box-shadow:0 0 0 1px rgba(40,30,0,.55)}
.track .dot.hollow{background:var(--paper);border:2px solid var(--muted);box-shadow:none}
.taxon{border-bottom:1px solid var(--grid)}.taxon .rrow{border-bottom:none}
.taxon .ex{font-size:14px;color:var(--ink-2);line-height:1.7;margin:0 0 8px}
ul.facts{margin:6px 0 4px;padding:0;list-style:none;font-size:14.5px;color:var(--ink-2);line-height:1.7}
ul.facts li{padding-left:14px;position:relative}
ul.facts li:before{content:"";position:absolute;left:2px;top:.75em;width:5px;height:5px;border-radius:50%;background:var(--muted)}
/* problems */
article.problem{border:1px solid var(--line);border-radius:4px;padding:18px 22px 14px;margin:16px 0}
article.problem>header{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px}
article.problem .advice{background:var(--accent-wash);margin:14px -22px -14px;padding:2px 22px 10px;border-radius:0 0 4px 4px}
article.problem .advice dl.kv dt,article.problem .advice dl.kv dd{border-top-color:#d3e3de}
.signal{padding:12px 0 4px;border-top:1px solid var(--grid)}
.signal:first-of-type{border-top:none}
.signal .hd{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;font-weight:650}
/* microbiome */
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px;margin:12px 0}
.tile{padding:12px 14px;border:1px solid var(--line);border-radius:4px}
.tile .k{font-size:13px;color:var(--muted);line-height:1.5}
.tile .v{font-size:20px;font-weight:650;line-height:1.35;margin:2px 0}
.tile .r{font-size:12.5px;color:var(--muted)}
.seg{display:flex;height:12px;gap:2px;margin:12px 0 8px}
.seg span{border-radius:4px;min-width:3px}
.seglegend{display:flex;flex-wrap:wrap;gap:4px 16px;font-size:13.5px;color:var(--ink-2)}
.seglegend span{display:inline-flex;align-items:center;gap:6px}.seglegend i{width:10px;height:10px;border-radius:2px;display:inline-block}
.split{display:grid;grid-template-columns:10em minmax(80px,1fr) 13em;gap:12px;align-items:center;font-size:14px;padding:6px 0;border-bottom:1px solid var(--grid)}
.split:last-of-type{border-bottom:none}
.split .seg{margin:0;height:10px}
.split .c{color:var(--ink-2);font-variant-numeric:tabular-nums;text-align:right;font-size:13px}
.dots{letter-spacing:2px;color:var(--accent);white-space:nowrap}
/* schedule */
table.sched{width:100%;border-collapse:collapse;font-size:13.5px;margin:6px 0 4px;border-top:1.5px solid var(--ink);border-bottom:1.5px solid var(--ink)}
table.sched th{font-weight:600;font-size:12.5px;padding:8px 4px;border-bottom:1px solid var(--ink);text-align:center;white-space:nowrap}
table.sched th:first-child,table.sched td:first-child{text-align:left;padding-left:0}
table.sched td{padding:9px 4px;border-bottom:1px solid var(--grid);text-align:center;vertical-align:middle}
table.sched tr:last-child td{border-bottom:none}
table.sched td.c{border-left:1px dashed var(--grid);width:11%}
.mk{display:inline-block;width:12px;height:12px;border-radius:50%;background:var(--accent);vertical-align:middle}
.mk.re{background:var(--paper);border:2px solid var(--accent)}
.legend{display:flex;flex-wrap:wrap;gap:4px 18px;font-size:13px;color:var(--muted);margin:6px 0 12px}
.legend span{display:inline-flex;align-items:center;gap:6px}
.legend i{display:inline-block;width:16px;height:8px;border-radius:4px}
/* appendix */
details.fold{border-top:1px solid var(--line)}
details.fold>summary{list-style:none;cursor:pointer;padding:12px 0;display:flex;align-items:center;gap:10px;font-weight:600}
details.fold>summary::-webkit-details-marker{display:none}
details.fold>summary:before{content:"";width:7px;height:7px;border-right:2px solid var(--muted);border-bottom:2px solid var(--muted);transform:rotate(-45deg);transition:transform .15s}
details.fold[open]>summary:before{transform:rotate(45deg)}
details.fold>summary .r{margin-left:auto;font-weight:400;color:var(--muted);font-size:13.5px}
details.fold .body{padding:0 0 14px}
dl.glossary{display:grid;grid-template-columns:9em 1fr;gap:8px 16px;font-size:14px;margin:6px 0}
dl.glossary dt{font-weight:600}dl.glossary dd{margin:0;color:var(--ink-2)}
ol.refs{padding-left:2.2em;font-size:13px;line-height:1.65;color:var(--ink-2)}
ol.refs li{margin-bottom:7px;overflow-wrap:anywhere}
ol.refs a{white-space:nowrap}
.totop{position:fixed;right:20px;bottom:20px;width:40px;height:40px;border-radius:50%;background:var(--paper);border:1px solid var(--line);display:grid;place-items:center;color:var(--ink-2);text-decoration:none;box-shadow:0 2px 8px rgba(0,0,0,.08)}
@media (max-width:1140px){
  .shell{grid-template-columns:minmax(0,860px);gap:0;padding:0 0 56px}
  nav.toc{position:sticky;top:0;z-index:5;background:rgba(236,238,237,.96);backdrop-filter:blur(8px);border-bottom:1px solid var(--line);padding:6px 10px;overflow-x:auto;white-space:nowrap}
  nav.toc .who{display:none}
  nav.toc ol{display:flex;gap:2px}
  nav.toc ol ol{display:none}
  nav.contents{display:none}
  nav.toc a{padding:6px 10px;border-left:none;border-bottom:2px solid transparent;white-space:nowrap}
  nav.toc a.active{border-bottom-color:var(--accent)}
  main{border-left:none;border-right:none;padding:36px 40px 48px}
}
@media (max-width:720px){
  body{font-size:15px}
  main{padding:24px 16px 40px}
  header.cover h1{font-size:26px}
  h2{font-size:21px}h3{font-size:17.5px}
  dl.idcard{grid-template-columns:auto minmax(0,1fr)}
  dl.idcard dd.wide{grid-column:auto}
  .scores{grid-template-columns:1fr}
  .stats{grid-template-columns:repeat(2,1fr)}
  .stats div:nth-child(3){border-left:none;padding-left:0}
  .stats div:nth-child(n+3){border-top:1px solid var(--grid)}
  .scores>svg{max-width:320px;margin:0 auto}
  .meter{grid-template-columns:1fr auto auto;gap:3px 10px}
  .meter .bar{grid-column:1/-1;grid-row:2}
  .rrow{grid-template-columns:1fr auto;gap:2px 10px}
  .rrow .track{grid-column:1/-1;grid-row:2}
  dl.kv{grid-template-columns:1fr}
  dl.kv dt{padding:8px 0 0}
  dl.kv dd{border-top:none;padding:2px 0 8px}
  table.tbl thead{display:none}
  table.tbl,table.tbl tbody,table.tbl tr,table.tbl td{display:block}
  table.tbl tr{padding:10px 0;border-bottom:1px solid var(--grid)}
  table.tbl tr:last-child{border-bottom:none}
  table.tbl td{border:none;padding:1px 0}
  table.tbl td[data-l]:before{content:attr(data-l) "：";color:var(--muted);font-size:13px}
  table.tbl td.n{display:none}
  .split{grid-template-columns:1fr auto}
  table.sched th:first-child,table.sched td:first-child{width:38%}
  table.sched th{font-size:11px;padding:6px 1px;white-space:normal}
  table.sched td.c{width:auto}
  .split .seg{grid-column:1/-1;grid-row:2}
  table.sched{font-size:12.5px}
  article.problem{padding:14px 14px 12px}
  article.problem .advice{margin:12px -14px -12px;padding:2px 14px 8px}
  dl.glossary{grid-template-columns:1fr}dl.glossary dd{margin-bottom:8px}
  nav.toc{mask-image:linear-gradient(90deg,#000 85%,transparent);-webkit-mask-image:linear-gradient(90deg,#000 85%,transparent)}
}
@page{size:A4;margin:16mm 15mm 18mm}
@page{@bottom-center{content:counter(page);font-size:9pt;color:#666}}
@media print{
  body{background:#fff;font-size:10.5pt}
  .shell{display:block;padding:0}
  nav.toc,.totop{display:none}
  main{border:none;padding:0}
  nav.contents{display:block;margin-bottom:8pt}
  section.chapter{padding-top:22pt}
  h3.domain,table.sched,.tiles,.splits{break-inside:avoid}
  h3.domain{break-after:avoid}
  .cap.fig{break-before:avoid}
  details.fold>summary{display:none}
  details.fold{border-top:none}
  details.fold>.body{display:block}
  .rrow,.taxon,.tile,table.tbl tr,table.sched tr,.scores,.stats,article.problem>header{break-inside:avoid}
  dl.kv dt,dl.kv dd,ul.facts li,ul.pts li,.lead,.gist{break-inside:avoid}
  article.problem{border-left:none;border-right:none;border-radius:0;padding-left:0;padding-right:0}
  article.problem .advice{margin-left:0;margin-right:0}
  h2,h3,h4,h5,.blk,.cap{break-after:avoid}
  a{color:inherit;text-decoration:none}
}
"""

_SCRIPT = """
document.querySelectorAll('a[href^="#"]').forEach(function(a){a.addEventListener('click',function(){
  var t=document.querySelector(a.getAttribute('href'));while(t){if(t.tagName==='DETAILS'){t.open=true;}t=t.parentElement;}});});
window.addEventListener('beforeprint',function(){document.querySelectorAll('details').forEach(function(d){d.open=true;});});
(function(){var links={};document.querySelectorAll('nav.toc a').forEach(function(a){links[a.getAttribute('href').slice(1)]=a;});
if(!('IntersectionObserver' in window))return;var io=new IntersectionObserver(function(es){es.forEach(function(e){
if(e.isIntersecting&&links[e.target.id]){Object.values(links).forEach(function(l){l.classList.remove('active');});
links[e.target.id].classList.add('active');if(window.innerWidth<=1140){
links[e.target.id].scrollIntoView({block:'nearest',inline:'center'});}}});},{rootMargin:'-20% 0px -70% 0px'});
document.querySelectorAll('section.chapter').forEach(function(s){io.observe(s);});})();
"""

_ICONS = {
    'critical': '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 1 11 10.5H1Z" fill="currentColor"/></svg>',
    'serious': '<svg viewBox="0 0 12 12" aria-hidden="true"><circle cx="6" cy="6" r="5" fill="currentColor"/>'
    '<rect x="5.2" y="2.6" width="1.6" height="4.4" rx=".8" fill="#fff"/><circle cx="6" cy="8.9" r=".9" fill="#fff"/></svg>',
    'warn': '<svg viewBox="0 0 12 12" aria-hidden="true"><circle cx="6" cy="6" r="4.6" fill="none" stroke="currentColor" '
    'stroke-width="1.8"/><circle cx="6" cy="6" r="1.6" fill="currentColor"/></svg>',
    'good': '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M2.2 6.3 4.9 9 9.8 3.4" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
}
_FILL = {'critical': 'var(--critical)', 'serious': 'var(--serious)', 'warn': 'var(--warn)', 'good': 'var(--good)'}
_PHYLUM_COLORS = ('#2a78d6', '#eb6834', '#1baf7a', '#6250d6', '#e87ba4')

# Plain definitions of abbreviations and terms the report uses (textbook level).
_GLOSSARY = (
    ('BMI', '体重指数', '体重（千克）除以身高（米）的平方。中国成人 24.0–27.9 为超重，28.0 及以上为肥胖。'),
    ('HOMA-IR', '胰岛素抵抗指数', '由空腹血糖和空腹胰岛素计算，数值越高，说明身体对胰岛素越不敏感。'),
    ('HOMA-β', '胰岛β细胞功能指数', '由空腹血糖和空腹胰岛素计算，反映胰岛分泌胰岛素的能力。'),
    ('HbA1c', '糖化血红蛋白', '反映过去 2–3 个月的平均血糖水平。'),
    ('C肽', 'C肽', '胰岛分泌胰岛素时等量释放的物质，用来评估胰岛的分泌功能。'),
    ('OGTT', '口服葡萄糖耐量试验', '空腹喝下定量葡萄糖后分次测血糖，用来发现早期的糖代谢异常。'),
    ('LDL-C', '低密度脂蛋白胆固醇', '常说的“坏胆固醇”，是动脉粥样硬化的主要危险因素。'),
    ('HDL-C', '高密度脂蛋白胆固醇', '常说的“好胆固醇”，帮助把胆固醇运回肝脏处理。'),
    ('non-HDL-C', '非高密度脂蛋白胆固醇', '总胆固醇减去高密度脂蛋白胆固醇，代表所有致动脉硬化的胆固醇。'),
    ('FIB-4', '肝纤维化指数', '由年龄、转氨酶和血小板计算，用于初步判断肝脏纤维化的风险。'),
    (
        'TI-RADS',
        '甲状腺结节超声分级',
        '按超声表现给甲状腺结节分级，级别越高越需要警惕；3 类属于低度可疑，一般定期复查即可。',
    ),
    ('粪便隐血', '粪便隐血试验', '检测粪便中肉眼看不见的少量血液，是结直肠癌筛查的常用方法。'),
    ('相对丰度', '相对丰度', '某种菌在全部检出微生物中所占的比例。'),
    ('GMWI2', '肠道微生物组健康指数', '根据菌群组成计算的综合指数，正值偏向健康状态，负值提示偏离健康状态。'),
    ('F/B', '厚壁菌门与拟杆菌门比值', '两大优势菌门的比例，反映菌群整体结构的偏向。'),
    ('短链脂肪酸', '短链脂肪酸', '膳食纤维经肠道菌群发酵产生的乙酸、丙酸、丁酸等，参与维持肠道屏障和代谢健康。'),
    ('肠型', '肠型', '按优势菌属把肠道菌群分成的几种类型，与长期饮食结构有关。'),
)


def _e(value) -> str:
    return html.escape('' if value is None else str(value))


def _num(value):
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _fmt(number) -> str:
    if number is None:
        return ''
    text = f'{number:.4f}'.rstrip('0').rstrip('.')
    return text if text not in {'-0', ''} else '0'


def _refs(node) -> str:
    numbers = node.get('evidenceRefs') if isinstance(node, dict) else None
    if not numbers:
        return ''
    return '<span class="cite">' + ''.join(f'<a href="#ref-{n}">[{n}]</a>' for n in numbers) + '</span>'


def _paras(value, node=None, *, css='') -> str:
    """Authored paragraphs, followed by the node's citation numbers."""
    if not isinstance(value, str) or not value.strip():
        return ''
    parts = [part.strip() for part in re.split(r'\n\s*\n', value) if part.strip()]
    tail = _refs(node) if node is not None else ''
    klass = f' class="{css}"' if css else ''
    return ''.join(f'<p{klass}>{_e(part)}{tail if i == len(parts) - 1 else ""}</p>' for i, part in enumerate(parts))


def _first_sentence(text, limit=90) -> str:
    """The first sentence of a text, shortened only at a clause boundary."""
    text = str(text or '').strip()
    match = re.match(r'(.{8,}?[。！？；])', text)
    sentence = match[1] if match else text
    if len(sentence) <= limit:
        return sentence
    cut = max(sentence.rfind(mark, 0, limit) for mark in '，、；：')
    return (sentence[:cut] if cut >= 8 else sentence[:limit].rstrip('0123456789.')) + '…'


def _kind(text) -> str:
    """Status kind of a label: critical / serious / warn / good / neutral (one mapping everywhere)."""
    text = str(text or '')
    if re.search(r'部分|存在|偏移|组合变化|弱项', text):
        return 'warn'
    if re.search(r'优先查因|高风险|阳性|极低', text):
        return 'critical'
    if re.search(r'重点关注|重点管理|中高风险|过量|肥胖|失调|不健康', text):
        return 'serious'
    if re.search(r'需要关注|中风险|近期复评|偏[高低]|降低|升高|异常|检出|超重|较弱', text):
        return 'warn'
    if re.search(r'关注度低|正常|低风险|丰富|未检出|健康|达标', text):
        return 'good'
    return 'neutral'


def _chip(text, kind=None) -> str:
    if text in (None, ''):
        return ''
    kind = kind or _kind(text)
    icon = _ICONS.get(kind, '')
    return f'<span class="chip {kind}">{icon}{_e(text)}</span>'


def _score_kind(score) -> str:
    return 'serious' if score < 60 else 'warn' if score < 80 else 'good'


_CODE_BRACKET = re.compile(r'\s*[（(][A-Za-z][A-Za-z0-9ⅠⅡⅢⅣ\-/·.\s]{0,12}[)）]')


_READER_TERMS = (('AST/ALT', '谷草/谷丙转氨酶比值'), ('/HP', '/高倍视野'), ('I 度失调', 'Ⅰ度失调'))


def _reader_terms(text) -> str:
    text = str(text or '')
    for old, new in _READER_TERMS:
        text = text.replace(old, new)
    return text


def _display_name(name) -> str:
    """Reader name of an item: no English code in brackets, no system suffixes."""
    text = _CODE_BRACKET.sub('', _reader_terms(name))
    text = re.sub(r'\s*及中国成人分级|检查异常(?:待复核)?$|^体成分测量|判定$', '', text)
    text = re.sub(r'(?<=[\u4e00-\u9fff])\s+[A-Za-z][A-Za-z0-9\-]{1,8}$', '', text)
    text = re.sub(r'\s+[A-Za-z][A-Za-z0-9\-]{1,8}\s+(?=[\u4e00-\u9fff])', '', text)
    return text.strip() or str(name or '')


def _tidy_number(text) -> str:
    """Trim trailing zeros of decimals in a printed value or range ("10.0000~25.0000" -> "10~25")."""

    def trim(match):
        value = match[0]
        if '.' not in value:
            return value
        whole, fraction = value.split('.')
        fraction = fraction.rstrip('0')
        if len(fraction) > 3 and whole not in {'0', '-0'}:
            fraction = fraction[:2]
        return whole + ('.' + fraction if fraction else '')

    return re.sub(r'-?\d+\.\d+', trim, str(text or ''))


# ---------- reference ranges ----------

_RANGE = re.compile(r'(-?\d+(?:\.\d+)?)\s*[~～\-–—至]\s*(-?\d+(?:\.\d+)?)')
_LIMIT = re.compile(r'([<>≤≥＜＞]=?)\s*(-?\d+(?:\.\d+)?)')


def _parse_reference(text):
    """(low, high) of a printed reference; one side may be None."""
    text = str(text or '')
    match = _RANGE.search(text)
    if match:
        low, high = float(match[1]), float(match[2])
        return (low, high) if high > low else None
    match = _LIMIT.search(text)
    if match:
        bound = float(match[2])
        return (None, bound) if match[1][0] in '<≤＜' else (bound, None)
    return None


_BAND = (32.0, 58.0)  # the reference range always spans this part of the track


def _position(value, reference, log=False):
    """Track position (0–100) and how far outside the range the value is."""
    low, high = reference
    scale = (lambda x: math.log10(max(x, 1e-5))) if log else (lambda x: x)  # noqa: E731
    if low is None:
        low = high / 10 if log else 0.0
    if high is None:
        high = low * 10 if log else (low * 2 if low > 0 else 1.0)
    a, b = scale(low), scale(high)
    width = (b - a) or 1.0
    position = _BAND[0] + (scale(value) - a) / width * (_BAND[1] - _BAND[0])
    return max(2.0, min(98.0, position)), low, high


def _track(value, reference, *, log=False, kind='warn', label='') -> str:
    """Bullet scale: reference band in the middle, dot = this result."""
    if value is None or reference is None:
        return '<div class="track"></div>'
    if log and value <= 0:
        return (
            f'<div class="track" role="img" aria-label="{_e(label)}：未检出"><span class="band" '
            f'style="left:{_BAND[0]}%;width:{_BAND[1] - _BAND[0]}%"></span>'
            '<span class="dot hollow" style="left:3%"></span></div>'
        )
    position, low, high = _position(value, reference, log)
    within = low <= value <= high
    fill = _FILL['good'] if within else _FILL.get(kind, _FILL['warn'])
    return (
        f'<div class="track" role="img" aria-label="{_e(label)}"><span class="band" style="left:{_BAND[0]}%;'
        f'width:{_BAND[1] - _BAND[0]}%"></span><span class="dot" style="left:{position:.1f}%;background:{fill}"></span></div>'
    )


_PIECE = re.compile(
    r'^(?:[^：:；;]{1,40}[：:]\s*)??(?P<name>[^：:；;]{1,40})[：:]\s*(?P<value>[<>≤≥]?-?\d+(?:\.\d+)?)\s*(?P<unit>[^（(；;]*?)\s*'
    r'[（(](?:参考)?(?P<ref>[^；;）)]*)[；;]?(?P<flag>[^）)]*)[）)]'
)


def _pieces(value):
    """Split a result text on top-level semicolons (not inside parentheses)."""
    pieces, depth, current = [], 0, []
    for char in str(value or ''):
        if char in '（(':
            depth += 1
        elif char in '）)':
            depth = max(0, depth - 1)
        if char in '；;' and depth == 0:
            pieces.append(''.join(current).strip())
            current = []
            continue
        current.append(char)
    pieces.append(''.join(current).strip())
    return [piece.strip('；; ') for piece in pieces if piece.strip('；; ')]


def _indicator_rows(value):
    """Numeric results with a printed reference inside a finding's result text."""
    rows, others = [], []
    for piece in _pieces(value):
        match = _PIECE.match(piece)
        number = _num(match['value']) if match else None
        reference = _parse_reference(match['ref']) if match else None
        if match and number is not None and reference is not None:
            rows.append(
                {
                    'name': match['name'].strip(),
                    'value': number,
                    'unit': match['unit'].strip(),
                    'ref': match['ref'].strip(),
                    'reference': reference,
                    'flag': match['flag'].strip(),
                }
            )
        else:
            others.append(piece)
    return rows, others


def _range_row(name, value, unit, ref_text, reference, flag, *, log=False, sub='', extra='') -> str:
    kind = _kind(flag) if flag else 'warn'
    numeric = isinstance(value, (int, float))
    shown = _tidy_number(_fmt(value)) if numeric else _e(value)
    unit = re.sub(r'(?<![A-Za-z])(kg/m|cm|m)2(?![0-9A-Za-z])', r'\1²', str(unit or ''))
    name = _display_name(name)
    flag_html = _chip(flag, kind) if flag and _kind(flag) != 'good' else ''
    ratio = ''
    if numeric and reference:
        low, high = reference
        if high and value > high * 1.5:
            ratio = f'<small class="ratio">约为上限的 {value / high:.1f} 倍</small>'
        elif low and 0 < value < low / 1.5:
            ratio = f'<small class="ratio">约为下限的 {value / low:.0%}</small>'
    if numeric and log and value <= 0:
        shown, unit = '未检出', ''
    label = f'{name} {shown}{unit}，参考 {_tidy_number(ref_text)}'
    return (
        f'<div class="rrow"><div class="nm">{_e(name)}{f"<small>{_e(sub)}</small>" if sub else ""}</div>'
        f'{_track(value if numeric else None, reference, log=log, kind=kind, label=label)}'
        f'<div class="val"><b>{shown}</b> {_e(unit)} {flag_html}<small>参考 {_e(_tidy_number(ref_text))}</small>{ratio}</div></div>{extra}'
    )


# ---------- itemised text ----------

_ITEM = re.compile(r'^(?P<title>[^：:，。；！？\s]{2,14})：(?P<text>\S.*)$', re.S)
_CN = '一二三四五六七八九十'


def _items(value):
    """(title, text) pairs of an authored cell; the title is '' for a plain paragraph."""
    rows = []
    for part in re.split(r'\n\s*\n', value) if isinstance(value, str) else ():
        part = part.strip()
        if not part:
            continue
        match = _ITEM.match(part)
        if match:  # a second generic heading inside the text is not content
            text = re.sub(r'^(?:总体|整体|综合|总的)?(?:结论|判断|评价|评估|概述|小结)：\s*', '', match['text'].strip())
            rows.append((match['title'], text))
        else:
            rows.append(('', part))
    return rows


def _prose(value, node=None, *, lead=False) -> str:
    """An authored cell: itemised paragraphs as labelled rows, plain ones as text.

    ``lead`` sets the conclusion item apart. A long paragraph without items is
    shown sentence by sentence so that no cell is a wall of text.
    """
    rows = _items(value)
    if not rows:
        return ''
    tail = _refs(node) if node is not None else ''
    out = []
    opening = re.search(r'结论|判断|总评|概述|格局', rows[0][0]) or (not rows[0][0] and len(rows) > 1)
    if lead and opening and len(rows[0][1]) <= 240:
        out.append(f'<p class="lead">{_e(rows[0][1])}{tail if len(rows) == 1 else ""}</p>')
        rows = rows[1:]
    if not rows:
        return ''.join(out)
    last = len(rows) - 1
    titled = sum(1 for title, _ in rows if title)
    if titled == len(rows) or (len(rows) >= 3 and titled >= len(rows) - 1):
        cells = ''.join(
            f'<dt>{_e(title)}</dt><dd>{_e(text)}{tail if index == last else ""}</dd>'
            for index, (title, text) in enumerate(rows)
        )
        out.append(f'<dl class="kv">{cells}</dl>')
        return ''.join(out)
    for index, (title, text) in enumerate(rows):
        text = (title + '：' if title else '') + text
        end = tail if index == last else ''
        sentences = [part.strip() for part in re.findall(r'[^。！？]+[。！？]?', text) if part.strip()]
        if len(text) > 170 and len(sentences) > 2:
            out.append('<ul class="pts">' + ''.join(f'<li>{_e(part)}</li>' for part in sentences) + f'</ul>{end}')
        else:
            out.append(f'<p>{_e(text)}{end}</p>')
    return ''.join(out)


class _Captions:
    """Running numbers of tables and figures."""

    def __init__(self):
        self.tables = self.figures = 0

    def table(self, text) -> str:
        self.tables += 1
        return f'<p class="cap">表 {self.tables}　{_e(text)}</p>'

    def figure(self, text) -> str:
        self.figures += 1
        return f'<p class="cap fig">图 {self.figures}　{_e(text)}</p>'


def _level(label, score=None):
    """Attention level of a domain as (chip text, kind, note)."""
    label = str(label or '')
    kind = _score_kind(score) if score is not None else _kind(label)
    if '资料有限' in label:
        return re.sub(r'[（(].*?[)）]', '', label) or '关注度低', 'neutral', '相关检查不全'
    return label, kind, ''


# ---------- the report's content, gathered by problem and domain ----------

_WHEN = re.compile(
    r'(尽快|近期|立即|两周内|\d+\s*[~～\-–至]\s*\d+\s*(?:周|个月|年)(?:内|后)?|\d+\s*(?:周|个月|年)(?:内|后|左右)?|'
    r'[一二两三四五六]个月(?:内|后)?|半年(?:内|后)?|一年(?:内|后)?|每\s*\d*\s*(?:周|个月|年))'
)
_STOPS = (('2周内', 0.5), ('1个月内', 1.0), ('3个月内', 3.0), ('6个月', 6.0), ('12个月', 12.0))
_DIGITS = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '半': 0.5}


def _due(text):
    """(months until first due, repeat interval in months or None) of a time limit."""
    text = str(text or '')
    unit = {'天': 1 / 30, '日': 1 / 30, '周': 0.25, '个月': 1.0, '月': 1.0, '年': 12.0}
    repeat = re.search(
        r'每\s*(\d+(?:\.\d+)?|[一二两三四五六半])?(?:\s*[~～\-–至]\s*\d+(?:\.\d+)?)?\s*(周|个月|年)', text
    )
    if repeat:
        count = repeat[1]
        number = float(count) if count and count[0].isdigit() else _DIGITS.get(count, 1)
        months = number * unit[repeat[2]]
        return months, months
    if re.search(r'立即|尽快|近期', text):
        return 0.5, None
    span = re.search(
        r'(\d+(?:\.\d+)?|[一二两三四五六半])\s*(?:[~～\-–至]\s*(\d+(?:\.\d+)?))?\s*(天|日|周|个月|年)', text
    )
    if span:
        count = span[2] or span[1]
        number = float(count) if count[0].isdigit() else _DIGITS.get(count, 1)
        return number * unit[span[3]], None
    if '半年' in text:
        return 6.0, None
    return None, None


def _schedule_marks(when, later):
    """Column -> mark of one schedule row: the first measure, then its rechecks."""

    def column(months):
        return next((index for index, (_, stop) in enumerate(_STOPS) if stop >= months), len(_STOPS) - 1)

    marks = {}
    months, repeat = _due(when)
    if months is not None:
        marks[column(months)] = 'first'
        later = later or (when if repeat else '')
    # A further examination that depends on a result has no date of its own.
    months, repeat = (None, None) if re.search(r'阳性|异常时|必要时|[如若]|出现', str(later or '')) else _due(later)
    if months is not None:
        if repeat:
            for index, (_, stop) in enumerate(_STOPS):
                if stop >= months and abs(stop / repeat - round(stop / repeat)) < 1e-6:
                    marks.setdefault(index, 're')
        else:
            marks.setdefault(column(months), 're')
    return marks


def _first_measure(item):
    """(time limit, action, clinic, later rechecks) of a problem's first measure.

    The cell is written as "时限；首要事项；科室；后续复查". A cell that does not
    follow the form is still read sensibly: the part that is a short time
    limit, the part that names a clinic, and the rest.
    """
    text = str((item.get('direction') or {}).get('followupAction') or '').strip('。；; ')
    parts = [part.strip() for part in re.split(r'[；;]', text) if part.strip()]
    if parts:
        when = parts.pop(0) if len(parts[0]) <= 10 and _due(parts[0])[0] is not None and len(parts) > 1 else ''
        action = parts.pop(0) if parts else ''
        clinic = next(
            (
                part
                for part in parts
                if len(part) <= 12
                and re.search(r'科|门诊|健康管理', part)
                and not re.search(r'复查|评估|随诊|检查|[，,]', part)
            ),
            '',
        )
        if clinic:
            parts.remove(clinic)
        if not when:
            found = _WHEN.search(action)
            when = found[1] if found else ''
        return when, action, clinic, '；'.join(parts)
    description = str((item.get('step') or {}).get('description') or '')
    first = _items(description)[0][1] if _items(description) else ''
    sentence = re.split(r'[。；]', first)[0]
    when = None if re.search(r'出现|如果|若|一旦|就医|急诊', sentence) else _WHEN.search(sentence)
    return (
        (when[1] if when else ''),
        _first_sentence(first, 44),
        str((item.get('finding') or {}).get('system') or ''),
        '',
    )


def _model(data):
    """Domains with their problems and microbiome features; each problem appears once."""
    overview = data.get('healthOverview') or {}
    medical = data.get('medicalAssessment') or {}
    risk = data.get('diseaseRiskAssessment') or {}
    conclusion = data.get('comprehensiveConclusion') or {}
    findings = [row for row in medical.get('findings') or () if row.get('name')]
    by_name = {row['name']: row for row in findings}
    key = {row.get('name'): row for row in overview.get('keyFindings') or ()}
    direction = {row.get('name'): row for row in conclusion.get('priorityDirections') or ()}
    steps = {row.get('name'): row for row in conclusion.get('nextSteps') or ()}
    matrix = {row.get('code'): row for row in risk.get('overviewMatrix') or ()}
    chart = {row.get('code'): row for row in overview.get('assessmentChart') or ()}
    ranking = risk.get('riskRanking') or ()

    def problem(name, finding=None, risk_row=None):
        return {
            'name': name,
            'finding': finding or {},
            'risk': risk_row or {},
            'key': key.get(name) or {},
            'direction': direction.get(name) or {},
            'step': steps.get(name) or {},
        }

    domains, placed = [], set()
    for category in risk.get('categories') or ():
        code = category.get('code')
        rows = [row for row in ranking if str(row.get('code') or '').startswith(f'templateRisk:{code}:')]
        score = _num((matrix.get(code) or {}).get('score'))
        if score is None:
            score = _num((chart.get(code) or {}).get('score'))
        problems = []
        for row in rows:
            if row.get('sourceStatus') or not row.get('name'):
                continue
            placed.add(row['name'])
            problems.append(problem(row['name'], by_name.get(row['name']), row))
        domains.append(
            {
                'code': code,
                'name': category.get('name'),
                'score': score,
                'level': (matrix.get(code) or {}).get('attentionLevel')
                or (chart.get(code) or {}).get('assessmentLevel'),
                'theme': category.get('attentionBasis'),
                'basis': matrix.get(code) or {},
                'summary': category.get('clinicalCorrelation') or {},
                'problems': problems,
                'signals': [row for row in rows if row.get('sourceStatus') and row.get('name')],
            }
        )
    for row in chart.values():  # a scored domain without a category still shows its score
        if row.get('code') not in {domain['code'] for domain in domains} and row.get('name'):
            domains.append(
                {
                    'code': row.get('code'),
                    'name': row.get('name'),
                    'score': _num(row.get('score')),
                    'level': row.get('assessmentLevel'),
                    'theme': None,
                    'basis': {},
                    'summary': {},
                    'problems': [],
                    'signals': [],
                }
            )
    domains.sort(key=lambda domain: (domain['score'] is None, domain['score'] or 0))
    others = [problem(row['name'], row) for row in findings if row['name'] not in placed]
    ordered = [item for domain in domains for item in domain['problems']] + others
    for index, item in enumerate(ordered, 1):
        item['anchor'] = f'p{index}'
        item['domain'] = next((domain['name'] for domain in domains if item in domain['problems']), '')
    rank = {name: row.get('rank') or 99 for name, row in key.items()}
    priorities = sorted((item for item in ordered if item['name'] in key), key=lambda item: rank[item['name']])
    return {'domains': domains, 'others': others, 'problems': ordered, 'priorities': priorities}


# ---------- pieces ----------


def _results(value) -> str:
    """Results of one problem: numeric ones against their range, the rest as a list."""
    indicators, others = _indicator_rows(value)
    seen, rows = set(), []
    for item in indicators:  # the same measurement under two names is drawn once
        if (item['value'], item['unit']) not in seen:
            seen.add((item['value'], item['unit']))
            rows.append(
                _range_row(item['name'], item['value'], item['unit'], item['ref'], item['reference'], item['flag'])
            )
    pieces = []
    for piece in others:
        piece = _reader_terms(piece)
        if piece.startswith(('体检结论', '检测结果和建议')):
            continue
        piece = re.sub(r'^问卷[：:]\s*', '症状与病史：', piece)
        piece = re.sub(r'(?:体检结论|内科问诊记录)[：:]\s*', '', piece)
        imaging = re.match(r'^[^：:]{1,20}[：:]\s*(?:超声提示|诊断意见|检查所见|超声所见|诊断提示)[：:]\s*(.+)$', piece)
        piece = imaging[1] if imaging else piece
        piece = re.sub(r'(?<![A-Za-z])(kg/m|cm|m)2(?![0-9A-Za-z])', r'\1²', _tidy_number(piece))
        piece = re.sub(r'(?<=\d)\s*\*\s*(?=\d)', '×', _CODE_BRACKET.sub('', piece)).strip('。；; ')
        if len(piece) >= 2 and piece not in pieces:
            pieces.append(piece)
    charts = f'<div class="ranges">{"".join(rows)}</div>' if rows else ''
    facts = '<ul class="facts">' + ''.join(f'<li>{_e(piece)}</li>' for piece in pieces) + '</ul>' if pieces else ''
    return charts + facts


def _problem(item) -> str:
    finding, key, risk = item['finding'], item['key'], item['risk']
    when = _first_measure(item)[0]
    chips = _chip(key.get('assessmentLevel')) + (f'<span class="when">{_e(when)}</span>' if when else '')
    out = [
        f'<article class="problem" id="{item["anchor"]}"><header><h4>{_e(_reader_terms(item["name"]))}</h4>{chips}</header>'
    ]
    if key.get('summary'):
        out.append(f'<p class="gist">{_e(key["summary"])}{_refs(key)}</p>')
    results = _results(finding.get('value'))
    if results:
        out.append('<h5>检查结果</h5>' + results)
    if finding.get('explanation'):
        out.append('<h5>评估意见</h5>' + _prose(finding['explanation'], finding))
    if risk.get('explanation'):
        out.append('<h5>风险评估</h5>' + _prose(risk['explanation'], risk))
    if finding.get('sourceAdvice'):
        out.append('<div class="advice"><h5>管理建议</h5>' + _prose(finding['sourceAdvice']) + '</div>')
    out.append('</article>')
    return ''.join(out)


def _taxon_row(row, *, note='') -> str:
    value = _num(row.get('value'))
    return (
        '<div class="taxon">'
        + _range_row(
            row.get('name') or row.get('scientificName'),
            value if value is not None else row.get('value'),
            row.get('unit') or '',
            row.get('referenceRange') or '—',
            _parse_reference(row.get('referenceRange')),
            row.get('sourceStatus') or row.get('assessmentLevel'),
            log=True,
        )
        + note
        + '</div>'
    )


def _signals(rows, taxa, cap, domain) -> str:
    """Disease directions that only gut-microbiome features point to, as one table."""
    out = [
        cap.table(f'{domain}方面的肠道菌群相关特征'),
        '<table class="tbl"><thead><tr><th>相关疾病</th><th>相关菌群（本次结果）</th><th>评估</th></tr></thead><tbody>',
    ]
    for row in rows:
        level = re.sub(r'^菌群评估[：:]\s*', '', str(row.get('sourceStatus') or ''))
        members = []
        for indicator in row.get('relatedIndicators') or ():
            known = {
                **(taxa.get(indicator.get('name')) or {}),
                **{k: v for k, v in indicator.items() if v not in (None, '')},
            }
            value = _num(known.get('value'))
            shown = (
                '未检出'
                if value is not None and value <= 0
                else _tidy_number(f'{known.get("value")}{known.get("unit") or ""}')
            )
            state = known.get('sourceStatus') or known.get('assessmentLevel')
            reference = f'，参考 {_tidy_number(known.get("referenceRange"))}' if known.get('referenceRange') else ''
            members.append(
                f'<li>{_e(known.get("name"))}：{_e(shown)}{f"（{_e(state)}{_e(reference)}）" if state else ""}</li>'
            )
        listing = f'<ul class="facts">{"".join(members)}</ul>' if members else '—'
        notes = ''.join(
            f'<p>{f"<b>{_e(title)}</b>　" if title else ""}{_e(text)}</p>'
            for title, text in _items(row.get('explanation'))
        )
        out.append(
            f'<tr><td class="nw" data-l="相关疾病"><b>{_e(row.get("name"))}</b><br>{_chip(level, "neutral") if level else ""}</td>'
            f'<td data-l="相关菌群">{listing}</td><td class="notes" data-l="评估">{notes}{_refs(row)}</td></tr>'
        )
    out.append('</tbody></table>')
    return ''.join(out)


def _radar(domains) -> str:
    """Seven domain scores at a glance; the meter list beside it carries the exact figures."""
    rows = [(domain['name'], domain['score']) for domain in domains if domain['score'] is not None]
    if len(rows) < 3:
        return ''
    count, cx, cy, radius = len(rows), 180.0, 150.0, 96.0

    def point(index, value):
        angle = -math.pi / 2 + 2 * math.pi * index / count
        return cx + radius * value / 100 * math.cos(angle), cy + radius * value / 100 * math.sin(angle)

    out = ['<svg viewBox="0 0 360 300" role="img" aria-label="健康领域评分雷达图">']
    for ring in (20, 40, 60, 80, 100):
        path = ' '.join(f'{x:.1f},{y:.1f}' for x, y in (point(index, ring) for index in range(count)))
        dash = ' stroke-dasharray="3 3"' if ring in (60, 80) else ''
        out.append(f'<polygon points="{path}" fill="none" stroke="#d5d9d6" stroke-width="1"{dash}/>')
    for index in range(count):
        x, y = point(index, 100)
        out.append(f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" stroke="#e7e9e7" stroke-width="1"/>')
    path = ' '.join(f'{x:.1f},{y:.1f}' for x, y in (point(index, score) for index, (_, score) in enumerate(rows)))
    out.append(
        f'<polygon points="{path}" fill="#0d5f52" fill-opacity=".12" stroke="#0d5f52" stroke-width="2" stroke-linejoin="round"/>'
    )
    for index, (name, score) in enumerate(rows):
        x, y = point(index, score)
        fill = (
            _FILL.get(_score_kind(score), '#0d5f52')
            .replace('var(--serious)', '#ec835a')
            .replace('var(--warn)', '#fab219')
            .replace('var(--good)', '#0ca30c')
        )
        out.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{fill}" stroke="#fff" stroke-width="2"><title>{_e(name)} {score:g} 分</title></circle>'
        )
        lx, ly = point(index, 100)
        dx = lx - cx
        anchor = 'middle' if abs(dx) < 12 else 'start' if dx > 0 else 'end'
        lx += 0 if anchor == 'middle' else 8 if dx > 0 else -8
        ly += -10 if ly < cy - radius * 0.8 else 16 if ly > cy + radius * 0.5 else 4
        short = re.sub(r'相关疾病$', '', str(name))
        out.append(
            f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="{anchor}" font-size="13.5" fill="#1b1f1d">{_e(short)}'
            f'<tspan font-weight="700" dx="4">{score:g}</tspan></text>'
        )
    out.append('</svg>')
    return ''.join(out)


def _meters(domains) -> str:
    out = ['<div class="meters">']
    for domain in domains:
        name, score = _e(domain['name']), domain['score']
        link = f'<a href="#d-{_e(domain["code"])}">{name}</a>'
        if score is None:
            out.append(
                f'<div class="meter">{link}<div class="bar"></div><span class="v muted">—</span><span>{_chip("资料不足", "neutral")}</span></div>'
            )
            continue
        label, kind, _ = _level(domain['level'], score)
        out.append(
            f'<div class="meter">{link}<div class="bar" role="img" aria-label="{name} {score:g} 分">'
            f'<i style="width:{score:g}%;background:{_FILL.get(kind, "#9aa09c")}"></i><s style="left:60%"></s><s style="left:80%"></s></div>'
            f'<span class="v">{score:g}</span><span>{_chip(label or None, kind)}</span></div>'
        )
    out.append('</div>')
    return ''.join(out)


# ---------- chapters ----------


def _cover(body, data, assessment=None) -> str:
    subject = body.get('subject') or {}
    title = (data.get('introduction') or {}).get('title') or '整体健康评估报告'
    if assessment is not None:
        timing = (assessment.get('overview') or {}).get('dataTiming') or {}
        sample = body.get('sample') or {}
        age = subject.get('age')
        profile = ' · '.join(str(value) for value in (subject.get('gender'), f'{age} 岁' if age is not None else None)
                             if value)
        clinical_date = timing.get('clinicalDate')
        if clinical_date and timing.get('clinicalInstitution'):
            clinical_date += f'（{timing["clinicalInstitution"]}）'
        sampling = timing.get('microbiomeSampleDate') or sample.get('sampleDate')
        received = timing.get('microbiomeReceivedDate')
        reported = timing.get('microbiomeReportDate') or sample.get('reportDate')
        gut_date = (
            ('肠道宏基因组采样日', sampling) if sampling else
            ('肠道菌群收样与报告日', reported) if received and received == reported else
            ('肠道菌群报告日', reported) if reported else
            ('肠道菌群收样日', received)
        )
        if gut_date[1] and sample.get('method'):
            gut_date = (gut_date[0], f'{gut_date[1]}（{sample["method"]}）')
        cells = (
            ('identity', '受检人姓名', subject.get('name')),
            ('identity', '性别 / 年龄', profile),
            ('identity', '菌群样本编号', sample.get('sampleId')),
            ('identity', '菌群原件登记标识', (assessment.get('microbiome') or {}).get('sampleContext', {})
             .get('reportedSubjectLabel')),
            ('dates', '临床体检基准日', clinical_date),
            ('dates', *gut_date),
            ('timing', '多源数据时效核验', f'体检与菌群采样间隔 {timing["daysBetween"]} 天'
             if timing.get('daysBetween') is not None else None),
        )
        rows = ''.join(f'<div class="{style}"><dt>{_e(label)}</dt><dd>{_e(value)}</dd></div>'
                       for style, label, value in cells if value)
        title = assessment.get('title') or '个体化全息健康与肠道微生态深度评估报告'
        return f'<header class="cover"><h1>{_e(title)}</h1><dl class="idcard">{rows}</dl></header>'
    scope = [
        name
        for name, present in (
            ('临床检查', (data.get('medicalAssessment') or {}).get('findings')),
            ('生活方式', (data.get('medicalAssessment') or {}).get('triggerAnalysis')),
            ('肠道微生态', (data.get('microbiomeAssessment') or {}).get('overallStructure')),
            ('营养与代谢功能', (data.get('nutritionAssessment') or {}).get('detailedMetabolism')),
        )
        if present
    ]
    cells = [
        ('姓名', subject.get('name')),
        ('性别', subject.get('gender')),
        ('年龄', f'{subject.get("age")} 岁' if subject.get('age') not in (None, '') else None),
        ('报告日期', str(body.get('reportDate') or body.get('generatedAt') or '')[:10]),
    ]
    rows = ''.join(f'<dt>{_e(label)}</dt><dd>{_e(value)}</dd>' for label, value in cells if value)
    if scope:
        rows += f'<dt>评估内容</dt><dd class="wide">{_e("、".join(scope))}</dd>'
    return (
        f'<header class="cover"><p class="kind">健康评估报告</p><h1>{_e(title)}</h1>'
        f'<dl class="idcard">{rows}</dl></header>'
    )


def _abnormal_rows(model):
    """Out-of-range numeric results of all problems, most deviant first."""
    rows, seen = [], set()
    for item in model['problems']:
        for row in _indicator_rows(item['finding'].get('value'))[0]:
            low, high = row['reference']
            value = row['value']
            outside = (high is not None and value > high) or (low is not None and value < low)
            if not outside and _kind(row['flag']) in {'good', 'neutral'}:
                continue
            if (value, row['unit']) in seen:
                continue
            seen.add((value, row['unit']))
            deviation = (
                (value - high) / abs(high)
                if high and value > high
                else (low - value) / abs(low)
                if low and value < low
                else 0.0
            )
            rows.append((deviation, row, item))
    rows.sort(key=lambda entry: -entry[0])
    return rows


def _conclusion(data, model, cap) -> str:
    overview = data.get('healthOverview') or {}
    out = ['<section class="chapter first" id="c1"><h2><span class="no">§NO§</span>评估结论</h2>']
    summary = overview.get('overallSummary') or {}
    abnormal = _abnormal_rows(model)
    scored = [domain for domain in model['domains'] if domain['name']]
    rated = [domain for domain in scored if domain['score'] is not None]
    normals = [row for row in (data.get('medicalAssessment') or {}).get('normalFindings') or () if row.get('name')]
    stats = [
        (len(model['priorities']), '项', '重点健康问题'),
        (len(abnormal), '项', '超出参考范围的指标'),
        (sum(1 for domain in rated if domain['score'] < 80), f'/ {len(rated)}', '需要关注的健康领域'),
        (len(normals), '项', '未见异常的检查'),
    ]
    out.append(
        '<div class="stats">'
        + ''.join(f'<div><b>{count}<small>{unit}</small></b><span>{label}</span></div>' for count, unit, label in stats)
        + '</div>'
    )
    out.append(_prose(summary.get('text'), summary, lead=True))
    if scored:
        out.append('<h3>（一）健康领域评分</h3>')
        radar = _radar(scored)
        out.append(f'<div class="scores">{radar}{_meters(scored)}</div>' if radar else _meters(scored))
        out.append(cap.figure('各健康领域评分（0–100 分，分数越高表示需要管理的问题越少；竖线为 60 分和 80 分界线）'))
        explanation = overview.get('overallScore') or {}
        out.append(_prose(explanation.get('explanation'), explanation))
    if model['priorities']:
        out.append('<h3>（二）重点健康问题</h3>' + cap.table('重点健康问题及首要措施'))
        out.append(
            '<table class="tbl"><thead><tr><th>序号</th><th>健康问题</th><th>主要依据</th><th>管理级别</th>'
            '<th>时限</th><th>首要措施</th></tr></thead><tbody>'
        )
        for index, item in enumerate(model['priorities'], 1):
            when, action, clinic, _ = _first_measure(item)
            basis = item['direction'].get('basis') or _first_sentence(item['key'].get('summary'), 46)
            domain = f'<br><span class="small muted">{_e(item["domain"])}</span>' if item['domain'] else ''
            where = f'<br><span class="small muted">{_e(clinic)}</span>' if clinic else ''
            out.append(
                f'<tr><td class="n">{index}</td><td data-l="健康问题"><a href="#{item["anchor"]}"><b>{_e(_reader_terms(item["name"]))}</b></a>{domain}</td>'
                f'<td data-l="主要依据">{_e(basis)}</td><td class="nw" data-l="管理级别">{_chip(item["key"].get("assessmentLevel"))}</td>'
                f'<td class="nw" data-l="时限">{_e(when) or "—"}</td><td data-l="首要措施">{_e(action)}{where}</td></tr>'
            )
        out.append('</tbody></table>')
    if abnormal:
        out.append('<h3>（三）主要异常指标</h3><div class="ranges">')
        for _, row, item in abnormal[:30]:
            out.append(
                _range_row(
                    row['name'],
                    row['value'],
                    row['unit'],
                    row['ref'],
                    row['reference'],
                    row['flag'],
                    sub=_reader_terms(item['name']),
                )
            )
        out.append(
            '</div>' + cap.figure('主要异常指标与参考范围（按偏离程度排列；浅绿色区段为参考范围，圆点为本次结果）')
        )
    out.append('</section>')
    return ''.join(out)


def _domains(data, model, cap) -> str:
    medical = data.get('medicalAssessment') or {}
    taxa = {}
    for row in (data.get('microbiomeAssessment') or {}).get('taxa') or ():
        taxa.setdefault(row.get('name'), row)
    out = ['<section class="chapter" id="c2"><h2><span class="no">§NO§</span>分领域评估</h2>']
    summary = ((data.get('healthOverview') or {}).get('summaries') or {}).get('medical') or {}
    out.append(_prose(summary.get('text'), summary, lead=True))
    out.append(
        '<p class="note">各领域按评分由低到高排列。每个健康问题集中列出检查结果、评估意见、风险评估和管理建议；随访安排见“健康管理方案”。</p>'
    )
    number = 0
    for domain in model['domains']:
        if not (
            domain['problems']
            or domain['signals']
            or (domain['summary'] or {}).get('text')
            or domain['basis'].get('basis')
        ):
            continue
        label, kind, note = _level(domain['level'], domain['score'])
        score = (
            f'<span class="score">{domain["score"]:g}<small>分</small></span>' if domain['score'] is not None else ''
        )
        theme = f'<span class="theme">管理主题：{_e(domain["theme"])}</span>' if domain['theme'] else ''
        out.append(
            f'<h3 class="domain" id="d-{_e(domain["code"])}"><span>（{_CN[number % 10]}）{_e(domain["name"])}</span>'
            f'{_chip(label or None, kind)}{_chip(note, "neutral") if note else ""}{score}{theme}</h3>'
        )
        number += 1
        basis = domain['basis']
        if basis.get('basis'):
            out.append(f'<p class="lead">{_e(basis["basis"])}{_refs(basis)}</p>')
        if (domain['summary'] or {}).get('text'):
            out.append('<h4 class="blk">综合评估</h4>' + _prose(domain['summary']['text'], domain['summary']))
        out.extend(_problem(item) for item in domain['problems'])
        if domain['signals']:
            out.append('<h4 class="blk">肠道菌群相关特征</h4>')
            out.append(_signals(domain['signals'], taxa, cap, re.sub(r'相关疾病$', '', str(domain['name']))))
    if model['others']:
        out.append(f'<h3 class="domain" id="d-other"><span>（{_CN[number % 10]}）其他检查发现</span></h3>')
        number += 1
        out.extend(_problem(item) for item in model['others'])
    normals = [row for row in medical.get('normalFindings') or () if row.get('name')]
    if normals:
        out.append(
            f'<h3 class="domain" id="d-normal"><span>（{_CN[number % 10]}）未见异常的检查</span></h3>'
            + cap.table('未见异常的检查项目')
        )
        out.append('<table class="tbl"><thead><tr><th>检查项目</th><th>结果</th></tr></thead><tbody>')
        for row in normals:
            value, name = _normal_value(row.get('value')), row.get('name')
            if not value and str(row.get('value') or '').startswith('问卷'):
                continue
            if value != _tidy_number(_reader_terms(row.get('value') or '')):
                name = re.sub(r'问诊与查体背景$', '查体', str(name))
            out.append(
                f'<tr><td class="nw" data-l="检查项目"><b>{_e(name)}</b></td><td data-l="结果">{_e(value) or "未见异常"}</td></tr>'
            )
        out.append('</tbody></table>')
    out.append('</section>')
    return ''.join(out)


_HISTORY = re.compile(r'^(?:问卷|既往史|长期用药史|家族史|运动锻炼情况|目前的吸烟状态|当前饮酒情况|睡眠充足程度)[：:]')


def _normal_value(value) -> str:
    """Result text of an unremarkable examination, without history items (those are assessed elsewhere)."""
    kept = []
    for sentence in re.findall(r'[^。]+。?', str(value or '')):
        parts = [
            part
            for part in re.split(r'[；;]', sentence.strip('。 '))
            if part.strip() and not _HISTORY.match(part.strip())
        ]
        if parts:
            kept.append('；'.join(part.strip() for part in parts) + '。')
    return _tidy_number(_reader_terms(''.join(kept)))


def _scale(value, levels) -> str:
    """A three-step scale with its label ("●●○ 中")."""
    if value not in levels:
        return _e(value or '—')
    filled = len(levels) - levels.index(value)
    return f'<span class="dots">{"●" * filled}{"○" * (len(levels) - filled)}</span> {_e(value)}'


def _lifestyle(data, cap) -> str:
    section = data.get('medicalAssessment') or {}
    triggers = [row for row in section.get('triggerAnalysis') or () if row.get('name')]
    conclusion = section.get('triggerConclusion') or {}
    if not triggers and not conclusion.get('text'):
        return ''
    out = ['<section class="chapter" id="c3"><h2><span class="no">§NO§</span>生活方式与影响因素</h2>']
    out.append(_prose(conclusion.get('text'), conclusion, lead=True))
    if triggers:
        out.append(cap.table('影响因素一览'))
        out.append(
            '<table class="tbl"><thead><tr><th>因素</th><th>现状</th><th>证据强度</th><th>可改变程度</th></tr></thead><tbody>'
        )
        for row in triggers:
            out.append(
                f'<tr><td class="nw" data-l="因素"><b>{_e(row.get("name"))}</b></td><td data-l="现状">{_e(row.get("observations") or "—")}</td>'
                f'<td class="nw" data-l="证据强度">{_scale(row.get("evidenceStrength"), ("强", "中", "弱"))}</td>'
                f'<td class="nw" data-l="可改变程度">{_scale(row.get("modifiability"), ("高", "中", "低"))}</td></tr>'
            )
        out.append('</tbody></table>')
        for row in triggers:
            if row.get('association'):
                out.append(f'<h3 class="blk">{_e(row.get("name"))}</h3>' + _prose(row['association'], row))
    out.append('</section>')
    return ''.join(out)


_TAXON_GROUPS = (
    ('beneficial', '有益菌'),
    ('opportunistic', '条件致病菌'),
    ('pathogenic', '致病菌'),
    ('fungi_pathogenic', '致病性真菌'),
    ('other', '其他菌'),
)


def _microbiome(data, cap) -> str:
    section = data.get('microbiomeAssessment') or {}
    if not any(section.get(key) for key in ('overallStructure', 'taxa', 'healthMetrics')):
        return ''
    out = ['<section class="chapter" id="c4"><h2><span class="no">§NO§</span>肠道微生态评估</h2>']
    summary = ((data.get('healthOverview') or {}).get('summaries') or {}).get('microbiome') or {}
    out.append(_prose(summary.get('text'), summary, lead=True))
    tiles = [row for row in section.get('overallStructure') or () if row.get('value') not in (None, '')]
    ranged = [
        row for row in tiles if _num(row.get('value')) is not None and _parse_reference(row.get('referenceRange'))
    ]
    tiles = [row for row in tiles if row not in ranged]
    structure = section.get('structureInterpretation') or {}
    if ranged or tiles or structure.get('text'):
        out.append('<h3>（一）整体结构</h3>')
    if ranged:
        out.append('<div class="ranges">')
        for row in ranged:
            out.append(
                _range_row(
                    row.get('name'),
                    _num(row.get('value')),
                    row.get('unit') or '',
                    row.get('referenceRange'),
                    _parse_reference(row.get('referenceRange')),
                    row.get('assessmentLevel') or row.get('sourceStatus'),
                )
            )
        out.append('</div>')
    if tiles:
        out.append('<div class="tiles">')
        for row in tiles:
            value = str(row.get('value'))
            state = row.get('assessmentLevel') or row.get('sourceStatus')
            if not state and re.search(r'异常|失调|偏', value):
                state = value
            shown = _e(_tidy_number(_reader_terms(value))) + (
                f' <small class="small muted">{_e(row.get("unit"))}</small>' if row.get('unit') else ''
            )
            reference = f'参考 {_e(_tidy_number(row.get("referenceRange")))}' if row.get('referenceRange') else ''
            chip = _chip(state) if state and state != value else ''
            out.append(
                f'<div class="tile"><div class="k">{_e(_display_name(row.get("name")))}</div><div class="v">{shown}</div>'
                f'<div class="r">{reference} {chip}</div></div>'
            )
        out.append('</div>')
    if ranged or tiles:
        out.append(cap.figure('肠道菌群整体结构指标'))
    out.append(_prose(structure.get('text'), structure))
    phyla = [row for row in section.get('phylumComposition') or () if _num(row.get('value')) is not None]
    if phyla:
        rest = max(0.0, 100 - sum(_num(row['value']) for row in phyla))
        parts = [(row.get('name'), _num(row['value']), row) for row in phyla[:4]]
        tail = sum(_num(row['value']) for row in phyla[4:]) + rest
        if tail > 0.05:
            parts.append(('其他', tail, {}))
        out.append('<h3>（二）菌门构成</h3><div class="seg" role="img" aria-label="菌门构成">')
        for index, (name, share, _) in enumerate(parts):
            color = _PHYLUM_COLORS[index % len(_PHYLUM_COLORS)] if name != '其他' else '#a8a7a1'
            out.append(f'<span style="flex:{share:.3f};background:{color}" title="{_e(name)} {share:.1f}%"></span>')
        out.append('</div><div class="seglegend">')
        for index, (name, share, row) in enumerate(parts):
            color = _PHYLUM_COLORS[index % len(_PHYLUM_COLORS)] if name != '其他' else '#a8a7a1'
            state = row.get('assessmentLevel') or row.get('sourceStatus')
            flag = f' {_chip(state)}' if state and _kind(state) != 'good' else ''
            reference = f'（参考 {_e(_tidy_number(row.get("referenceRange")))}）' if row.get('referenceRange') else ''
            out.append(f'<span><i style="background:{color}"></i>{_e(name)} {share:.1f}%{reference}{flag}</span>')
        out.append('</div>' + cap.figure('菌门构成（相对丰度）'))
    all_taxa = [row for row in section.get('taxa') or () if row.get('name') or row.get('scientificName')]
    metrics = [row for row in section.get('healthMetrics') or () if row.get('name')]
    if all_taxa or metrics:
        out.append('<h3>（三）各类菌群评估</h3>')
    counted = {}
    for row in all_taxa:
        state = str(row.get('sourceStatus') or row.get('assessmentLevel') or '')
        side = 'low' if re.search(r'偏低|降低', state) else 'high' if re.search(r'偏高|升高', state) else 'mid'
        counted.setdefault(row.get('category') or 'other', {'low': 0, 'mid': 0, 'high': 0})[side] += 1
    if counted:
        out.append('<div class="splits">')
        for code, title in _TAXON_GROUPS:
            count = counted.get(code)
            if not count:
                continue
            total = sum(count.values())
            segments = ''.join(
                f'<span style="flex:{count[side]};background:var(--{side})"></span>'
                for side in ('low', 'mid', 'high')
                if count[side]
            )
            out.append(
                f'<div class="split"><span>{title}<small class="muted">　{total} 种</small></span><div class="seg" role="img" '
                f'aria-label="{title}：偏低 {count["low"]}，正常 {count["mid"]}，偏高 {count["high"]}">{segments}</div>'
                f'<span class="c">偏低 {count["low"]}　正常 {count["mid"]}　偏高 {count["high"]}</span></div>'
            )
        out.append(
            '</div><div class="legend"><span><i style="background:var(--low)"></i>偏低</span><span><i style="background:var(--mid)"></i>正常</span>'
            '<span><i style="background:var(--high)"></i>偏高</span></div>'
            + cap.figure('各类菌相对参考范围的分布（按菌种数）')
        )
    for row in metrics:
        state = row.get('assessmentLevel') or row.get('sourceStatus') or row.get('value')
        details = [part.strip() for part in re.split(r'[；;]', str(row.get('details') or '')) if part.strip()]
        listing = (
            '<ul class="facts">' + ''.join(f'<li>{_e(_tidy_number(part))}</li>' for part in details) + '</ul>'
            if details
            else ''
        )
        out.append(
            f'<h4 class="blk">{_e(row["name"])} {_chip(state)}</h4>{listing}{_prose(row.get("explanation"), row)}'
        )
    signals = section.get('keyMicrobialSignals') or {}
    taxa, seen = [], set()
    for row in all_taxa:
        identity = (row.get('scientificName') or row.get('name') or '').casefold()
        state = str(row.get('sourceStatus') or row.get('assessmentLevel') or '')
        if identity in seen or not re.search(r'偏|异常|升高|降低', state):
            continue
        seen.add(identity)
        taxa.append(row)
    if taxa or signals.get('text'):
        out.append('<h3>（四）偏离参考范围的菌</h3>')
        out.append(_prose(signals.get('text'), signals, lead=True))
        state_of = lambda row: str(row.get('sourceStatus') or row.get('assessmentLevel'))  # noqa: E731
        trace = [row for row in taxa if re.search(r'偏高|升高', state_of(row)) and (_num(row.get('value')) or 0) < 0.01]
        groups = [
            ('偏低或未检出', [row for row in taxa if re.search(r'偏低|降低', state_of(row))]),
            ('偏高', [row for row in taxa if row not in trace and re.search(r'偏高|升高', state_of(row))]),
            ('偏高但含量极低（不足万分之一）', trace),
        ]
        for title, group in groups:
            if not group:
                continue
            out.append(f'<h4 class="blk">{_e(title)}（{len(group)} 种）</h4><div class="ranges">')
            for row in group:
                note = f'<p class="ex">{_e(row.get("explanation"))}{_refs(row)}</p>' if row.get('explanation') else ''
                out.append(_taxon_row(row, note=note))
            out.append('</div>')
        out.append(cap.figure('偏离参考范围的菌（对数刻度；空心点表示未检出）'))
    scfa = [row for row in section.get('shortChainFattyAcids') or () if _num(row.get('value')) is not None]
    if scfa:
        interpretation = section.get('scfaInterpretation') or {}
        out.append(
            '<h3>（五）短链脂肪酸合成能力</h3>'
            + _score_rows(scfa)
            + cap.figure('短链脂肪酸合成能力评分（浅绿色区段为正常区间）')
        )
        out.append(_prose(interpretation.get('text'), interpretation, lead=True))
    amr = section.get('antimicrobialResistance') or ()
    if amr:
        out.append('<h3>（六）耐药基因</h3>')
        for row in amr:
            parts = [part for part in re.split(r'[；;]', str(row.get('value') or '')) if part.strip()]
            table = [re.split(r'[：:]', part) for part in parts]
            if table and all(len(cells) >= 2 for cells in table):
                out.append(cap.table('各类抗生素耐药基因丰度'))
                out.append(
                    '<table class="tbl"><thead><tr><th>抗生素类别</th><th>丰度</th><th>判定</th></tr></thead><tbody>'
                    + ''.join(
                        f'<tr><td data-l="抗生素类别">{_e(cells[0])}</td><td class="num" data-l="丰度">{_e(_tidy_number(cells[1]))}</td>'
                        f'<td data-l="判定">{_chip(cells[2]) if len(cells) > 2 else ""}</td></tr>'
                        for cells in table
                    )
                    + '</tbody></table>'
                )
            out.append(_prose(row.get('explanation'), row))
    out.append('</section>')
    return ''.join(out)


def _bands(text):
    """Score bands printed with a function score, e.g. 正常:(25-75)."""
    return [
        (name, float(low), float(high))
        for name, low, high in re.findall(r'(极低|偏低|正常|偏高)[:：]\s*\((\d+)-(\d+)\)', str(text or ''))
    ]


def _score_rows(items) -> str:
    rows = []
    for item in items:
        value = _num(item.get('value'))
        if value is None:
            continue
        bands = _bands(item.get('referenceRange'))
        normal = next(((low, high) for name, low, high in bands if name == '正常'), None)
        state = item.get('assessmentLevel') or item.get('sourceStatus') or ''
        kind = _kind(state) if state else 'good'
        position = max(0.0, min(100.0, value))
        band = (
            f'<span class="band" style="left:{normal[0]:.0f}%;width:{normal[1] - normal[0]:.0f}%"></span>'
            if normal
            else ''
        )
        fill = _FILL['good'] if kind in {'good', 'neutral'} else _FILL.get(kind, _FILL['warn'])
        rows.append(
            f'<div class="rrow"><div class="nm">{_e(_display_name(item.get("name")))}</div><div class="track" role="img" '
            f'aria-label="{_e(item.get("name"))} {value:g} 分">{band}<span class="dot" style="left:{position:.0f}%;background:{fill}">'
            f'</span></div><div class="val"><b>{value:g}</b> 分 {_chip(state) if state and kind != "good" else ""}</div></div>'
        )
    return '<div class="ranges">' + ''.join(rows) + '</div>' if rows else ''


def _nutrition(data, cap) -> str:
    section = data.get('nutritionAssessment') or {}
    pathways = [row for row in (data.get('functionalPathways') or {}).get('pathways') or () if row.get('name')]
    groups = [*(section.get('detailedMetabolism') or ()), *(section.get('vitaminsAndMinerals') or ())]
    extra = [
        {
            'name': '氨基酸',
            'items': section.get('aminoAcidsAndMetabolites') or (),
            'explanation': section.get('aminoMetaboliteInterpretation'),
        },
        {
            'name': '神经递质相关',
            'items': section.get('neurotransmitters') or (),
            'explanation': section.get('neurotransmitterInterpretation'),
        },
    ]
    groups = [*groups, *[item for item in extra if item['items']]]
    major = [row for row in section.get('majorMetabolism') or () if row.get('name')]
    if not (groups or major or pathways):
        return ''
    out = ['<section class="chapter" id="c5"><h2><span class="no">§NO§</span>营养与代谢功能评估</h2>']
    summary = ((data.get('healthOverview') or {}).get('summaries') or {}).get('nutrition') or {}
    out.append(_prose(summary.get('text'), summary, lead=True))
    out.append(
        '<p class="note">本部分的评分反映肠道菌群对相应营养物质的代谢潜力（0–100 分），不代表体内营养素含量。</p>'
    )
    if major:
        out.append('<h3>（一）功能大类判定</h3>' + cap.table('营养与代谢功能大类判定'))
        out.append(
            '<table class="tbl"><thead><tr><th>功能类别</th><th>判定</th><th>功能类别</th><th>判定</th></tr></thead><tbody>'
        )
        for start in range(0, len(major), 2):
            cells = ''
            for row in major[start : start + 2]:
                state = row.get('assessmentLevel') or row.get('sourceStatus') or row.get('value')
                cells += f'<td data-l="功能类别">{_e(_display_name(row.get("name")))}</td><td class="nw" data-l="判定">{_chip(state)}</td>'
            out.append(f'<tr>{cells}{"<td></td><td></td>" if len(major[start : start + 2]) == 1 else ""}</tr>')
        out.append('</tbody></table>')
        overall = section.get('nutritionInterpretation') or {}
        if overall.get('text'):
            out.append('<h4 class="blk">与饮食和检查结果的对照</h4>' + _prose(overall['text'], overall))
    if groups:
        out.append('<h3>（二）分项功能评分</h3>')
        for group in groups:
            items = group.get('items') or ()
            flagged = sum(1 for item in items if _kind(item.get('assessmentLevel')) not in {'good', 'neutral'})
            explanation = group.get('explanation') or {}
            out.append(
                f'<h4 class="blk">{_e(group.get("name"))}（{len(items)} 项，其中 {flagged} 项偏离）</h4>{_score_rows(items)}'
            )
            out.append(_prose(explanation.get('text'), explanation))
        out.append(cap.figure('分项功能评分（浅绿色区段为正常区间，圆点为本次评分）'))
    if pathways:
        summary = (data.get('functionalPathways') or {}).get('summary') or {}
        out.append('<h3 id="c6">（三）功能通路</h3>')
        out.append(_prose(summary.get('text'), summary, lead=True))
        for row in pathways:
            state = row.get('assessmentLevel') or row.get('sourceStatus')
            related = '；'.join(
                f'{_display_name(metric.get("name"))} {metric.get("value")}{metric.get("unit") or ""}'
                + (f'（{metric.get("assessmentLevel")}）' if metric.get('assessmentLevel') else '')
                for metric in row.get('relatedMetrics') or ()
                if metric.get('name') and metric.get('value') not in (None, '')
            )
            facts = f'<ul class="facts"><li>相关指标：{_e(related)}</li></ul>' if related else ''
            out.append(
                f'<h4 class="blk">{_e(row["name"])} {_chip(state)}</h4>{facts}{_prose(row.get("healthMeaning"), row)}'
            )
    out.append('</section>')
    return ''.join(out)


def _plan(data, model, cap) -> str:
    section = data.get('comprehensiveConclusion') or {}
    planned = [
        item
        for item in model['priorities']
        if item['step'].get('description') or item['direction'].get('followupAction')
    ]
    overall = section.get('overallConclusion') or {}
    if not planned and not overall.get('text'):
        return ''
    out = ['<section class="chapter" id="c7"><h2><span class="no">§NO§</span>健康管理方案</h2>']
    out.append(_prose(overall.get('text'), overall, lead=True))
    if planned:
        out.append('<h3>（一）随访与复查日程</h3>')
        head = ''.join(f'<th>{label}</th>' for label, _ in _STOPS)
        # The last column exists only when some problem has no dated measure.
        undated = any(not _schedule_marks(*_first_measure(item)[::3]) for item in planned)
        extra = '<th>按专科约定</th>' if undated else ''
        out.append(f'<table class="sched"><thead><tr><th>健康问题</th>{head}{extra}</tr></thead><tbody>')
        for item in planned:
            when, _, _, later = _first_measure(item)
            marks = _schedule_marks(when, later)
            cells = []
            for index, (label, _) in enumerate(_STOPS):
                kind = marks.get(index)
                mark = (
                    f'<span class="mk{" re" if kind == "re" else ""}" role="img" '
                    f'aria-label="{label}{"复查" if kind == "re" else ""}"></span>'
                    if kind
                    else ''
                )
                cells.append(f'<td class="c">{mark}</td>')
            tail = '<span class="mk" role="img" aria-label="按专科约定"></span>' if not marks else ''
            tail = f'<td class="c">{tail}</td>' if undated else ''
            label = f'<br><span class="small muted">{_e("；".join(part for part in (when, later) if part))}</span>'
            out.append(
                f'<tr><td><a href="#{item["anchor"]}">{_e(_reader_terms(item["name"]))}</a>{label}</td>'
                f'{"".join(cells)}{tail}</tr>'
            )
        out.append('</tbody></table>')
        out.append(
            '<div class="legend"><span><span class="mk"></span>首要事项的完成时限</span>'
            '<span><span class="mk re"></span>后续复查</span></div>' + cap.figure('随访与复查日程（自报告日期起算）')
        )
        out.append(cap.table('随访与复查计划'))
        out.append(
            '<table class="tbl"><thead><tr><th>时限</th><th>首要事项</th><th>科室</th><th>后续复查</th>'
            '<th>对应健康问题</th></tr></thead><tbody>'
        )
        ordered = sorted(
            planned, key=lambda item: (_due(_first_measure(item)[0])[0] is None, _due(_first_measure(item)[0])[0] or 0)
        )
        for item in ordered:
            when, action, clinic, later = _first_measure(item)
            out.append(
                f'<tr><td class="nw" data-l="时限"><b>{_e(when) or "按专科约定"}</b></td><td data-l="首要事项">{_e(action)}</td>'
                f'<td class="nw" data-l="科室">{_e(clinic) or "—"}</td><td data-l="后续复查">{_e(later) or "—"}</td>'
                f'<td data-l="对应健康问题"><a href="#{item["anchor"]}">{_e(_reader_terms(item["name"]))}</a></td></tr>'
            )
        out.append('</tbody></table>')
        out.append('<h3>（二）分项随访安排</h3>')
        for item in planned:
            step = item['step']
            if step.get('description'):
                out.append(
                    f'<h4 class="blk">{_e(_reader_terms(item["name"]))} {_chip(item["key"].get("assessmentLevel"))}</h4>'
                    + _prose(step['description'], step)
                )
    out.append('</section>')
    return ''.join(out)


def _appendix(data) -> str:
    out = [
        '<section class="chapter" id="c8"><h2><span class="no">附录</span></h2><h3>（一）图例与评分说明</h3>'
        '<dl class="kv"><dt>领域评分</dt><dd>各健康领域评分范围为 0–100 分，分数越高表示该领域当前需要管理的问题越少：'
        '低于 60 分为重点关注，60–79 分为需要关注，80 分及以上为关注度低。标有“相关检查不全”的领域，评分所依据的针对性检查较少。</dd>'
        '<dt>参考范围图</dt><dd>浅绿色区段为参考范围，圆点为本次结果；圆点落在区段内为绿色，偏离参考范围时按偏离性质着色，空心点表示未检出。'
        '肠道菌群丰度采用对数刻度。</dd>'
        '<dt>管理级别</dt><dd>“优先查因”指需在短期内完成进一步检查以明确原因；“重点管理”指需制定目标并定期监测；'
        '“持续管理”指按约定间隔随访；“近期复评”指需在近期复查后重新评估。</dd>'
        '<dt>文献标注</dt><dd>正文中的方括号数字对应本附录“参考文献”的序号。</dd></dl>'
    ]
    text = json.dumps(data, ensure_ascii=False)
    used = [(term, name, meaning) for term, name, meaning in _GLOSSARY if term in text]
    known = {name for _, name, _ in used} | {term for term, _, _ in used}
    for row in (data.get('appendices') or {}).get('indicatorStandards') or ():
        name = _display_name(row.get('name'))
        meaning = str(row.get('meaning') or '')
        if name and name not in known and meaning and not re.search(r'模型|输出|推断|检测方', meaning):
            used.append((name, name, meaning))
            known.add(name)
    if used:
        body = ''.join(
            f'<dt>{_e(term)}{"" if term == name else "<br><span class=small>" + _e(name) + "</span>"}</dt><dd>{_e(meaning)}</dd>'
            for term, name, meaning in used
        )
        out.append(
            f'<h3>（二）名词解释</h3><details class="fold"><summary>展开名词解释<span class="r">{len(used)} 条</span></summary>'
            f'<div class="body"><dl class="glossary">{body}</dl></div></details>'
        )
    references = (data.get('appendices') or {}).get('referenceLiterature') or ()
    if references:
        out.append(
            f'<h3>（三）参考文献</h3><details class="fold" id="refs"><summary>展开参考文献<span class="r">{len(references)} 篇</span></summary>'
            '<div class="body"><ol class="refs">'
        )
        for row in references:
            authors = row.get('authors') or []
            lead = ', '.join(authors[:3]) + (' 等' if len(authors) > 3 else '')
            title = re.sub(r'<[^>]+>', '', str(row.get('title') or ''))
            parts = [part for part in (lead, title, row.get('journal'), row.get('year')) if part]
            link = ('https://doi.org/' + row['doi']) if row.get('doi') else row.get('url')
            grade = row.get('studyType')
            out.append(
                f'<li id="ref-{_e(row.get("number"))}" value="{_e(row.get("number"))}">{_e(". ".join(str(p) for p in parts))}.'
                + (f' <span class="muted">（{_e(grade)}）</span>' if grade else '')
                + (f' <a href="{_e(link)}" target="_blank" rel="noopener">查看原文</a>' if link else '')
                + '</li>'
            )
        out.append('</ol></div></details>')
    out.append('</section>')
    return ''.join(out)


def render_evaluation_html(body: dict) -> str:
    from modules.report.assessment_callback_view import assessment_view_for_rendering

    data = body.get('data') or {}
    assessment = assessment_view_for_rendering(data)
    if assessment is not None:
        if __package__:
            from .render_assessment_presentation import ASSESSMENT_CSS
            from .render_assessment_presentation import assessment_chapters
        else:
            from render_assessment_presentation import ASSESSMENT_CSS
            from render_assessment_presentation import assessment_chapters

        chapters = assessment_chapters(
            data, assessment, status=body.get('status'), partial_reasons=body.get('partialReasons') or (),
        )
        return _render_document(body, data, chapters, assessment=assessment, extra_css=ASSESSMENT_CSS)

    model, cap = _model(data), _Captions()
    # The integrated assessment and its plan come first; topical detail follows.
    builders = [
        ('c1', '评估结论', lambda: _conclusion(data, model, cap)),
        ('c2', '分领域评估', lambda: _domains(data, model, cap)),
        ('c3', '生活方式与影响因素', lambda: _lifestyle(data, cap)),
        ('c7', '健康管理方案', lambda: _plan(data, model, cap)),
        ('c4', '肠道微生态评估', lambda: _microbiome(data, cap)),
        ('c5', '营养与代谢功能评估', lambda: _nutrition(data, cap)),
        ('c8', '附录', lambda: _appendix(data)),
    ]
    chapters, number = [], 0
    for anchor, name, build in builders:  # built in reading order so that tables and figures number in order
        html_ = build()
        if not html_:
            continue
        if '§NO§' in html_:
            html_ = html_.replace('§NO§', _CN[number] + '、')
            name = f'{_CN[number]}、{name}'
            number += 1
        chapters.append((anchor, name, html_))
    sub = ''.join(
        f'<li><a href="#d-{_e(domain["code"])}">{_e(domain["name"])}</a></li>'
        for domain in model['domains']
        if domain['problems'] or domain['signals'] or (domain['summary'] or {}).get('text')
    )
    return _render_document(body, data, chapters, sub=sub)


def _render_document(body, data, chapters, *, sub='', assessment=None, extra_css='') -> str:
    subject = body.get('subject') or {}
    title = (data.get('introduction') or {}).get('title') or '整体健康评估报告'
    if assessment is not None:
        title = assessment.get('title') or '个体化全息健康与肠道微生态深度评估报告'
        main = _cover(body, data, assessment) + ''.join(html_ for _, _, html_ in chapters)
        return (
            '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{_e(title)}</title><style>{_CSS}{extra_css}</style></head><body>'
            f'<div class="assessment-shell"><main id="top" class="assessment">{main}</main></div></body></html>'
        )
    toc = ''.join(
        f'<li><a href="#{anchor}">{name}</a>{f"<ol>{sub}</ol>" if anchor == "c2" and sub else ""}</li>'
        for anchor, name, _ in chapters
    )
    date = str(body.get('reportDate') or body.get('generatedAt') or '')[:10]
    who = f'<div class="who"><b>{_e(subject.get("name") or "")}</b>{_e(title)}<br>{_e(date)}</div>'
    contents = f'<nav class="contents" aria-label="目录"><p>目录</p><ol>{toc}</ol></nav>'
    main = _cover(body, data, assessment) + contents + ''.join(html_ for _, _, html_ in chapters)
    return (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{_e(subject.get("name") or "")} {_e(title)}</title><style>{_CSS}{extra_css}</style></head><body>'
        f'<div class="shell"><nav class="toc" aria-label="目录">{who}<ol>{toc}</ol></nav>'
        f'<main id="top">{main}</main></div>'
        '<a class="totop" href="#top" aria-label="回到顶部"><svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">'
        '<path d="M3 10 8 5l5 5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
        f'</svg></a><script>{_SCRIPT}</script></body></html>'
    )


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        raise SystemExit('usage: render_evaluation_report.py callback.json report.html')
    body = json.loads(Path(args[0]).read_text(encoding='utf-8'))
    Path(args[1]).write_text(render_evaluation_html(body), encoding='utf-8')


if __name__ == '__main__':
    if not __package__:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    main()
