"""Render the six-chapter assessment (``assessment.v2``) as the printable sample.

This is the reference rendering given to the frontend team. It reads only the
callback fields described in ``wiki/report-assessment-v2-render.md`` and draws
each section with the component named by the content spec: range bars, system
cards, action cards and a follow-up schedule, a relationship graph, an evidence
matrix, macro-index and abundance scales, 0-100 score bars with their bands,
cluster cards, a lifestyle-by-cluster matrix and a three-stage roadmap.

It adds no medical judgement. Levels, bands and directions come from the
callback; when a field is absent the same deterministic parser that produced
it is applied to the row's own strings (``modules.report.assessment_chart_fields``).
"""

from __future__ import annotations

import math
import re
from copy import deepcopy
from datetime import date
from html import escape
from math import ulp
from unicodedata import normalize
from urllib.parse import parse_qsl
from urllib.parse import urlencode
from urllib.parse import urlsplit

from modules.report import assessment_chart_fields as charts
from modules.report.assessment_callback_view import ASSESSMENT_SECTIONS
from modules.report.assessment_microbiome_view import annotate_microbiome_chart_fields
from modules.report.assessment_microbiome_view import organism_assessment_display_group
from modules.report.assessment_presentation import _SYSTEMS
from modules.report.assessment_reading_labels import clinical_observation_display_name

ASSESSMENT_CSS = """
.assessment-shell{max-width:940px;margin:0 auto;padding:28px 18px 64px}
main.assessment{
  --ink:#1b1f1d;--ink-2:#3d433f;--muted:#5b615d;--line:#d3d8d4;--grid:#e6e9e6;--wash:#f4f5f3;--paper:#fff;
  --accent:#0d5f52;--accent-wash:#e7f0ed;--band:#cfe6d8;--band-edge:#6fa688;
  --focus:#ec835a;--focus-ink:#8a3413;--focus-wash:#fbe7dc;
  --borderline:#fab219;--borderline-ink:#6a4500;--borderline-wash:#fdf2d6;
  --normal:#0ca30c;--normal-ink:#1c5c1c;--normal-wash:#e6f3e4;
  --untested:#9aa09c;--untested-ink:#3f423f;--untested-wash:#eceeec;
  --p0:#d03b3b;--p0-ink:#9a2121;--p0-wash:#f9e1e0;
  --serif:"Noto Serif CJK SC","Source Han Serif SC","Songti SC",SimSun,serif;
  background:var(--paper);color:var(--ink);font:15px/1.72 var(--sans);padding:52px 56px 60px;
  border:1px solid var(--line)
}
.assessment ::selection{background:#cfe3dd}
.assessment h1,.assessment h2,.assessment h3{font-family:var(--serif);font-weight:700;letter-spacing:.01em;
  text-wrap:balance}
.assessment h2{font-size:24px;line-height:1.35;margin:0 0 14px;padding:0 0 10px;border-bottom:1.5px solid var(--ink)}
.assessment h3{font-size:18px;line-height:1.45;margin:32px 0 10px}
.assessment h4{font-size:15.5px;line-height:1.5;margin:0;font-weight:650}
.assessment h5{font-size:12.5px;line-height:1.5;margin:0 0 6px;font-weight:650;color:var(--muted);letter-spacing:.04em}
.assessment p{margin:0 0 9px}
.assessment section.chapter{padding-top:34px}
.assessment .as-lead{font-size:15.5px;line-height:1.8;margin:0 0 14px}
.assessment .as-muted{color:var(--muted)}
.assessment .as-small{font-size:13px;line-height:1.65}
.assessment .as-missing{color:var(--ink-2);padding:10px 14px;background:var(--wash);border-radius:6px;margin:8px 0 12px}
.assessment .as-note{padding:11px 15px;background:var(--accent-wash);border-radius:6px;margin:12px 0}
.assessment .as-note>p:last-child,.assessment .as-note>ul:last-child{margin-bottom:0}
.assessment .cite{font-size:11px;vertical-align:super;line-height:0;margin-left:1px;white-space:nowrap}
.assessment .cite a{text-decoration:none;padding:0 1px;color:var(--accent)}
.assessment .num{font-variant-numeric:tabular-nums}
/* cover */
.assessment header.as-cover{padding:0 0 22px;margin:0 0 6px;border-bottom:3px double var(--ink)}
.assessment header.as-cover .kind{font-size:13px;letter-spacing:.32em;color:var(--muted);margin:0 0 10px}
.assessment header.as-cover h1{font-size:30px;line-height:1.3;margin:0 0 20px}
.assessment dl.as-id{display:grid;grid-template-columns:repeat(2,auto minmax(0,1fr));gap:6px 14px;margin:0;
  font-size:14.5px}
.assessment dl.as-id dt{color:var(--muted);white-space:nowrap}
.assessment dl.as-id dd{margin:0;font-weight:600;font-variant-numeric:tabular-nums}
.assessment .as-timing{display:flex;flex-wrap:wrap;align-items:center;gap:6px 12px;margin:16px 0 0;padding:10px 14px;
  background:var(--wash);border-radius:6px;font-size:14px}
.assessment .as-timing b{font-weight:650}
/* status and priority chips */
.assessment .as-lv,.assessment .as-pri{display:inline-flex;align-items:center;gap:4px;padding:0 8px 0 6px;
  border-radius:3px;font-size:12.5px;line-height:1.8;font-weight:600;white-space:nowrap;vertical-align:middle}
.assessment .as-lv svg,.assessment .as-pri svg{width:11px;height:11px;flex:none}
.assessment .lv-focus{background:var(--focus-wash);color:var(--focus-ink)}
.assessment .lv-borderline{background:var(--borderline-wash);color:var(--borderline-ink)}
.assessment .lv-normal{background:var(--normal-wash);color:var(--normal-ink)}
.assessment .lv-notTested,.assessment .lv-none{background:var(--untested-wash);color:var(--untested-ink)}
.assessment .pri-p0{background:var(--p0-ink);color:#fff}
.assessment .pri-p1{background:var(--ink-2);color:#fff}
.assessment .pri-p2{background:var(--paper);color:var(--ink-2);box-shadow:inset 0 0 0 1px #8f9692}
.assessment .pri-p3{background:var(--paper);color:var(--muted);box-shadow:inset 0 0 0 1px var(--line)}
.assessment .as-when{display:inline-block;font-size:12.5px;line-height:1.75;padding:0 8px;
  border:1px solid var(--accent);
  color:var(--accent);border-radius:3px;white-space:normal;max-width:100%;overflow-wrap:anywhere}
/* tags carry a measured value; the dot carries the level */
.assessment .as-tags{display:flex;flex-wrap:wrap;gap:5px 5px;margin:2px 0}
.assessment .as-tag{display:inline-flex;align-items:baseline;gap:5px;padding:2px 8px 2px 7px;border-radius:3px;
  background:var(--wash);font-size:13px;line-height:1.6;color:var(--ink)}
.assessment .as-tag i{width:7px;height:7px;border-radius:50%;background:var(--untested);flex:none;align-self:center}
.assessment .as-tag b{font-weight:650;font-variant-numeric:tabular-nums}
.assessment .as-tag small{color:var(--muted);font-size:12px}
.assessment .as-tag.lv-focus{background:var(--focus-wash)}.assessment .as-tag.lv-focus i{background:var(--focus)}
.assessment .as-tag.lv-borderline{background:var(--borderline-wash)}
.assessment .as-tag.lv-borderline i{background:var(--borderline)}
.assessment .as-tag.lv-normal{background:var(--normal-wash)}.assessment .as-tag.lv-normal i{background:var(--normal)}
.assessment .as-tag.lv-focus,.assessment .as-tag.lv-borderline,.assessment .as-tag.lv-normal{color:var(--ink)}
.assessment .as-tag.plain{background:transparent;box-shadow:inset 0 0 0 1px var(--line)}
.assessment .as-tag.lv-none{background:transparent;box-shadow:inset 0 0 0 1px var(--line)}
.assessment .as-tag.lv-none i{background:#fff;box-shadow:inset 0 0 0 1.5px #7d837f}
.assessment .as-dir{display:inline-block;width:8px;height:10px;fill:currentColor;vertical-align:-1px}
.assessment .as-more{font-size:12.5px;color:var(--muted);align-self:center}
/* legend */
.assessment .as-legend{display:grid;grid-template-columns:auto minmax(0,1fr);gap:8px 14px;align-items:center;
  margin:16px 0 4px;padding:12px 16px;background:var(--wash);border-radius:6px;font-size:13px}
.assessment .as-legend>b{font-weight:650;color:var(--ink-2)}
.assessment .as-legend>div{display:flex;flex-wrap:wrap;align-items:center;gap:6px 14px}
.assessment .as-legend span.sw{display:inline-flex;align-items:center;gap:6px}
.assessment .as-legend .sw svg{height:18px;width:auto}
/* measured rows */
.assessment .as-mrows{margin:4px 0 6px}
.assessment .as-mrow{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,1fr) minmax(140px,2fr) minmax(0,1.1fr);
  gap:2px 14px;align-items:center;padding:7px 0;border-bottom:1px solid var(--grid);font-size:14px}
.assessment .as-mrows>.as-mrow:last-child{border-bottom:none}
.assessment .as-mrow .nm{line-height:1.45}
.assessment .as-mrow .nm small{display:block;color:var(--muted);font-size:12px}
.assessment .as-mrow .val{line-height:1.45}
.assessment .as-mrow .val b{font-size:15.5px;font-weight:650;font-variant-numeric:tabular-nums}
.assessment .as-mrow .val .as-lv{margin-left:4px}
.assessment .as-mrow .ref{font-size:12.5px;color:var(--ink-2);line-height:1.5;font-variant-numeric:tabular-nums}
.assessment .as-mrow .txt{grid-column:3/5;font-size:13.5px;color:var(--ink-2);line-height:1.6}
.assessment .as-mrow .gap{font-size:12.5px;color:var(--muted)}
.assessment .as-mrow .ch>svg,.assessment .as-axis svg{display:block;width:100%;height:auto;overflow:visible}
.assessment .as-axis{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,1fr) minmax(140px,2fr) minmax(0,1.1fr);
  gap:0 14px;font-size:11.5px;color:var(--muted);padding:2px 0 0}
.assessment .as-axis>div:nth-child(3){grid-column:3}
.assessment svg text{font-family:var(--sans)}
.assessment svg .trk{stroke:#dde1de;stroke-width:2;stroke-linecap:round}
.assessment svg .band{fill:var(--band);stroke:var(--band-edge);stroke-width:1}
.assessment svg .tgt{fill:var(--accent)}
.assessment svg .tk{font-size:11px;fill:var(--muted)}
.assessment svg .tkl{stroke:#c9cec9;stroke-width:1}
.assessment svg .dot{stroke:#fff;stroke-width:2}
.assessment svg .ring{fill:none;stroke:rgba(27,31,29,.5);stroke-width:.8}
.assessment svg .dot.lv-focus{fill:var(--focus)}.assessment svg .dot.lv-borderline{fill:var(--borderline)}
.assessment svg .dot.lv-normal{fill:var(--normal)}.assessment svg .dot.lv-notTested{fill:#7d837f}
.assessment svg .dot.lv-none{fill:#fff;stroke:#6b716d;stroke-width:1.8}
.assessment svg .hollow{fill:#fff;stroke:#5b615d;stroke-width:1.6}
.assessment svg .over{fill:#5b615d}
.assessment svg .bn{fill:var(--normal-wash);stroke:#a9d3a4;stroke-width:.8}
.assessment svg .blo{fill:#e5eefa;stroke:#a8c4e6;stroke-width:.8}
.assessment svg .blx{fill:#cfe0f5;stroke:#86abd9;stroke-width:.8}
.assessment svg .bhi{fill:#fdf0d8;stroke:#ebcf8f;stroke-width:.8}
.assessment svg .bhx{fill:#fbe0d2;stroke:#e6ad93;stroke-width:.8}
.assessment svg .bo{fill:#f1efe9;stroke:#cfcac0;stroke-width:.8}
.assessment svg .bt{font-size:10px;fill:var(--ink-2)}
/* panels of chapter three and four */
.assessment .as-panel{margin:16px 0 20px}
.assessment .as-panel-head{display:flex;flex-wrap:wrap;align-items:center;gap:4px 10px;padding:0 0 6px;
  border-bottom:1.5px solid var(--ink-2)}
.assessment .as-panel-head .as-lv{margin-left:auto}
.assessment .as-panel-head small{color:var(--muted);font-size:12.5px}
.assessment .as-normal{display:flex;gap:8px;font-size:13px;color:var(--ink-2);line-height:1.7;padding:7px 0 2px}
.assessment .as-normal svg{width:13px;height:13px;flex:none;margin-top:4px;color:var(--normal)}
.assessment .as-normal b{font-weight:650;color:var(--normal-ink);white-space:nowrap}
.assessment .as-interp{margin:9px 0 0;font-size:14.5px}
.assessment .as-interp p{margin:0 0 7px}
.assessment .as-refline{font-size:12px;color:var(--muted)}
/* system cards */
.assessment .as-systems{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin:12px 0}
.assessment .as-sys{border:1px solid var(--line);border-radius:8px;overflow:hidden;background:var(--paper)}
.assessment .as-sys>header{display:flex;align-items:center;gap:10px;padding:9px 12px;background:var(--wash)}
.assessment .as-sys.lv-focus>header{background:var(--focus-wash)}
.assessment .as-sys.lv-borderline>header{background:var(--borderline-wash)}
.assessment .as-sys.lv-normal>header{background:var(--normal-wash)}
.assessment .as-sys>header h4{font-size:15px;color:var(--ink)}
.assessment .as-sys>header .as-lv{margin-left:auto;background:rgba(255,255,255,.75)}
.assessment .as-sys .bd{padding:10px 12px 12px;font-size:13.5px}
.assessment .as-sys .bd p{margin:7px 0 0;color:var(--ink-2)}
/* action cards and schedule */
.assessment .as-actions{display:grid;gap:10px;margin:10px 0}
.assessment .as-act{border:1px solid var(--line);border-radius:8px;padding:12px 15px}
.assessment .as-act.p0{border-color:#e3b1ae}
.assessment .as-act>header{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;margin:0 0 4px}
.assessment .as-act dl{display:grid;grid-template-columns:5em minmax(0,1fr);gap:4px 12px;margin:8px 0 0;
  font-size:13.5px}
.assessment .as-act dt{color:var(--muted)}
.assessment .as-act dd{margin:0}
.assessment .as-act p{font-size:14px;margin:6px 0 0}
.assessment .as-gaps{margin:8px 0;padding:0;list-style:none}
.assessment .as-gaps li{display:grid;grid-template-columns:minmax(8em,auto) minmax(0,1fr);gap:4px 14px;padding:7px 0;
  border-bottom:1px solid var(--grid);font-size:14px}
.assessment .as-gaps li:last-child{border-bottom:none}
.assessment table.sched td:first-child{text-align:left}
.assessment table.sched th{font-family:var(--sans)}
/* graph */
.assessment figure.as-graph{margin:12px 0 8px}
.assessment figure.as-graph svg{display:block;width:100%;height:auto}
.assessment figure.as-graph .hub{fill:var(--accent)}
.assessment figure.as-graph .hub-t{fill:#fff;font-size:15px;font-weight:700}
.assessment figure.as-graph .hub-s{fill:#d9ebe5;font-size:12px}
.assessment figure.as-graph .edge{fill:none;stroke:#a9b1ac;stroke-width:1.5}
.assessment figure.as-graph .node{stroke-width:1.2}
.assessment figure.as-graph .node{fill:#fff}
.assessment figure.as-graph .node.pri-p0{stroke:var(--p0-ink);stroke-width:2}
.assessment figure.as-graph .node.pri-p1{stroke:var(--ink-2);stroke-width:1.6}
.assessment figure.as-graph .node.pri-p2{stroke:#8f9692}
.assessment figure.as-graph .node.pri-p3{stroke:var(--line)}
.assessment figure.as-graph .badge{rx:3}
.assessment figure.as-graph .badge.pri-p0{fill:var(--p0-ink)}
.assessment figure.as-graph .badge.pri-p1{fill:var(--ink-2)}
.assessment figure.as-graph .badge.pri-p2,.assessment figure.as-graph .badge.pri-p3{fill:#8f9692}
.assessment figure.as-graph .bid{font-size:12px;font-weight:700;fill:#fff}
.assessment figure.as-graph .ns{font-size:11.5px;fill:var(--ink-2)}
.assessment figure.as-graph .nid{font-size:13px;font-weight:700;fill:var(--ink)}
.assessment figure.as-graph .nt{font-size:13px;fill:var(--ink)}
.assessment figure.as-graph .np{font-size:11px;fill:var(--ink-2)}
.assessment figcaption{font-size:12.5px;color:var(--muted);margin:6px 0 0}
/* matrices */
.assessment table.as-matrix{width:100%;border-collapse:collapse;font-size:13.5px;line-height:1.6;margin:8px 0 12px;
  border-top:1.5px solid var(--ink);border-bottom:1.5px solid var(--ink);table-layout:fixed}
.assessment table.as-matrix th{text-align:left;font-weight:650;font-size:12.5px;color:var(--ink-2);
  padding:7px 10px 7px 0;
  border-bottom:1px solid var(--ink);vertical-align:bottom}
.assessment table.as-matrix td{padding:9px 10px 9px 0;border-bottom:1px solid var(--grid);vertical-align:top}
.assessment table.as-matrix tr:last-child td{border-bottom:none}
.assessment table.as-matrix td.c{text-align:center;padding-right:0}
.assessment table.as-matrix th.c{text-align:center;padding-right:0}
.assessment table.as-matrix .qa{display:block;color:var(--ink-2)}
.assessment table.as-matrix .qa+.qa{margin-top:3px}
.assessment .as-hit{display:inline-block;width:11px;height:11px;border-radius:50%;background:var(--accent)}
/* cluster cards */
.assessment .as-cluster{border:1px solid var(--line);border-radius:8px;margin:14px 0;overflow:hidden}
.assessment .as-cluster>header{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;padding:10px 14px;
  background:var(--wash)}
.assessment .as-cid{font-size:12.5px;font-weight:700;padding:0 7px;border-radius:3px;background:var(--ink);color:#fff;
  line-height:1.8}
.assessment .as-cluster>header h4{font-size:15.5px}
.assessment .as-cluster>header .as-pri{margin-left:auto}
.assessment .as-evidence{display:grid;grid-template-columns:repeat(3,minmax(0,1fr))}
.assessment .as-evidence>div{padding:10px 12px;border-top:1px solid var(--grid)}
.assessment .as-evidence>div+div{border-left:1px solid var(--grid)}
.assessment .as-cluster .notes{padding:10px 14px 4px;border-top:1px solid var(--grid);font-size:14.5px}
.assessment .as-cluster .notes h5{margin-top:2px}
/* roadmap */
.assessment .as-roadmap{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:14px 0}
.assessment .as-stage{border:1px solid var(--line);border-radius:8px;overflow:hidden}
.assessment .as-stage>header{padding:10px 13px;border-bottom:1px solid var(--grid);background:var(--wash)}
.assessment .as-stage>header .t{display:flex;align-items:baseline;gap:8px}
.assessment .as-stage>header .t b{font-family:var(--serif);font-size:19px}
.assessment .as-stage>header .t span{font-size:12.5px;color:var(--muted)}
.assessment .as-stage>header h4{font-size:13.5px;color:var(--ink-2);font-weight:600;margin-top:2px}
.assessment .as-stage ul{list-style:none;margin:0;padding:6px 13px 10px}
.assessment .as-stage li{padding:7px 0;border-bottom:1px solid var(--grid);font-size:13.5px;line-height:1.6}
.assessment .as-stage li:last-child{border-bottom:none}
.assessment .as-stage li strong{display:block;font-weight:650}
.assessment .as-stage p{padding:8px 13px 0;font-size:13.5px;color:var(--ink-2)}
.assessment .as-direction{margin:12px 0}
.assessment .as-direction h4{margin:0 0 6px}
.assessment .as-direction ol{margin:6px 0 0;padding-left:1.3em}
.assessment .as-direction li{margin:0 0 10px}
.assessment .as-scores{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));column-gap:28px;margin:4px 0 4px}
.assessment .as-srow{display:grid;grid-template-columns:minmax(0,7.5em) minmax(0,1fr) 3.2em;align-items:center;gap:8px;
  padding:3px 0;border-bottom:1px solid var(--grid);font-size:13px;line-height:1.5}
.assessment .as-srow>svg{display:block;width:100%;height:auto;overflow:visible}
.assessment .as-srow .v{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.assessment .as-srow[data-lv=borderline] .nm,.assessment .as-srow[data-lv=focus] .nm{font-weight:650}
.assessment .as-bandkey{display:flex;flex-wrap:wrap;gap:2px 14px;font-size:12px;color:var(--ink-2);margin:6px 0 2px}
.assessment .as-bandkey i{display:inline-block;width:14px;height:9px;border-radius:2px;margin-right:5px;
  vertical-align:0}
.assessment .as-bandkey i.bn{background:var(--normal-wash);border:1px solid #a9d3a4}
.assessment .as-bandkey i.blo{background:#e5eefa;border:1px solid #a8c4e6}
.assessment .as-bandkey i.blx{background:#cfe0f5;border:1px solid #86abd9}
.assessment .as-bandkey i.bhi{background:#fdf0d8;border:1px solid #ebcf8f}
.assessment .as-bandkey i.bhx{background:#fbe0d2;border:1px solid #e6ad93}
.assessment .as-bandkey i.bo{background:#f1efe9;border:1px solid #cfcac0}
.assessment .as-systems>.as-sys:last-child:nth-child(odd){grid-column:1/-1}
.assessment nav.as-toc{margin:18px 0 0;font-size:13.5px;line-height:1.75}
.assessment nav.as-toc ol{columns:2;column-gap:32px;margin:0;padding:0;list-style:none}
.assessment nav.as-toc a{color:var(--ink-2);text-decoration:none}
.assessment .as-layers{margin:6px 0 10px}
.assessment .as-layers h4{margin:12px 0 4px}
.assessment h4.as-sub{margin:20px 0 8px;font-size:15px}
.assessment ol.as-readings,.assessment ul.as-readings{list-style:none;margin:6px 0;padding:0}
.assessment .as-readings>li{margin:0 0 12px}
.assessment .as-readings>li>h4{margin:0 0 4px}
.assessment .as-readings>li>strong{display:block;margin:0 0 3px}
/* trends */
.assessment .as-trends{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px;margin:10px 0}
.assessment .as-trend{border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.assessment .as-trend svg{display:block;width:100%;height:auto}
.assessment .as-trend .line{fill:none;stroke:var(--accent);stroke-width:2;stroke-linejoin:round}
.assessment .as-trend .pt{fill:var(--accent);stroke:#fff;stroke-width:1.5}
/* references */
.assessment .as-references{margin-top:28px;padding-top:18px;border-top:1px solid var(--line)}
.assessment .as-reference-list{padding-left:2.2em;font-size:12.5px;color:var(--ink-2)}
.assessment .as-reference-list li{margin:0 0 8px;overflow-wrap:anywhere}
.assessment .as-reference-list a{white-space:nowrap}
@media screen and (max-width:720px){
  .assessment-shell{padding:0}
  main.assessment{padding:24px 14px;border:none}
  .assessment dl.as-id{grid-template-columns:auto minmax(0,1fr)}
  .assessment .as-systems,.assessment .as-roadmap,.assessment .as-evidence,.assessment .as-scores{
    grid-template-columns:minmax(0,1fr)}
  .assessment .as-evidence>div+div{border-left:none}
  .assessment .as-mrow{grid-template-columns:minmax(0,1fr) auto}
  .assessment .as-mrow>div:nth-child(3),.assessment .as-mrow .txt{grid-column:1/-1}
  .assessment .as-mrow .ref{grid-column:1/-1}
  .assessment .as-axis{display:none}
  .assessment table.as-matrix{table-layout:auto}
}
@media print{
  .assessment-shell{max-width:none;padding:0;margin:0}
  main.assessment{padding:0;border:none;font-size:10pt;line-height:1.62}
  .assessment *{print-color-adjust:exact;-webkit-print-color-adjust:exact}
  .assessment section.chapter{break-before:page;padding-top:0}
  .assessment section#as-preface{break-before:auto;padding-top:18pt}
  .assessment h2,.assessment h3,.assessment h4,.assessment h5,.assessment .as-panel-head{break-after:avoid-page}
  .assessment .as-cluster,.assessment .as-note,.assessment .as-roadmap,.assessment .as-direction>h4{
    break-inside:avoid}
  .assessment .as-sys,.assessment .as-act,.assessment .as-mrow,.assessment .as-stage,.assessment .as-legend,
  .assessment figure.as-graph,.assessment table.as-matrix tr,.assessment .as-trend,.assessment .as-gaps li,
  .assessment table.sched,.assessment .as-cluster>header,.assessment .as-evidence,.assessment .as-timing{
    break-inside:avoid}
  .assessment .as-panel-head+.as-mrows .as-mrow:first-child{break-before:avoid-page}
  .assessment .as-axis{break-after:avoid-page}
  .assessment .as-srow{break-inside:avoid}
  .assessment p,.assessment li{orphans:2;widows:2}
  .assessment .as-reference-list li{break-inside:avoid}
  .assessment a{color:inherit;text-decoration:none}
}
"""

_CLINICAL_CONCLUSION = re.compile(
    r'(?:^|(?<=[。；;\n]))\s*(?:'
    r'(?:检查结论|影像诊断|影像印象|诊断意见|结论)(?:\s*[：:]\s*|\s+|(?=[\u4e00-\u9fff]))'
    r'|(?:超声提示|提示)\s*[：:]\s*)'
)
_CLINICAL_IMAGE = re.compile(r'超声|彩超|影像|正位片|侧位片|X线|CT|MRI|磁共振', re.IGNORECASE)
_STATED_NORMAL_IMAGING = {'未见明显异常', '未见异常', '正常'}
_HEPATOBILIARY_ORGANS = re.compile(r'肝|胆囊|胆管|胆道|胆总管|胰腺|胰管|门静脉')
_OTHER_ABDOMINAL_ORGANS = re.compile(r'肾|脾|输尿管|集合系统|膀胱|前列腺|子宫|附件')
_LIFESTYLE_PROMPTS = (
    (r'^您?平时一周(?:大约)?有几天安排运动[?？]$', '平时运动频率'),
    (r'^您?平时连续坐着、不起身的时间(?:通常)?有多久[?？]$', '平时连续久坐时长'),
    (r'^您?平时的正餐(?:通常)?吃到哪些食物[?？](?:可多选。)?$', '平时正餐食物'),
    (r'^您?平时吃甜食或喝含糖饮料的频率更接近哪一种[?？]$', '平时甜食或含糖饮料频率'),
    (r'^您?平时临睡前吃夜宵的频率更接近哪一种[?？]$', '平时临睡前夜宵频率'),
    (r'^您?平时的饮酒频率更接近哪一种[?？]$', '平时饮酒频率'),
    (r'^您?最近通常每天睡多长时间[?？]$', '最近每日睡眠时长'),
)
_DOI = re.compile(r'10\.\d{4,9}/[^\s<>?#]+', re.IGNORECASE)
_TRACKING_KEYS = frozenset(
    {
        'af',
        'casa_token',
        'content',
        'dclid',
        'fbclid',
        'fr',
        'gclid',
        'guestaccesskey',
        'mc_cid',
        'mc_eid',
        'mi',
        'msclkid',
        'previousarticle',
        'ref',
        'referrer',
        'redirect',
        'search',
        'source',
        'target',
        'text1',
        'widget',
    }
)


def _text(value):
    if isinstance(value, (list, tuple)):
        return '、'.join(_text(item) for item in value)
    if value is None:
        return ''
    if isinstance(value, bool):
        return '是' if value else '否'
    return str(value)


def _e(value):
    return escape(_text(value))


def _value(row):
    value = _text(row.get('value')).strip()
    unit = _text(row.get('unit')).strip()
    if not value or not unit:
        return value
    # Some source serializers print a binary float tail after an otherwise
    # short percentage. Suppress only a lost precision of at most one ULP.
    if (unit in {'%', '％'} and re.fullmatch(r'[+-]?\d+\.\d{12,}', value)
            and re.search(r'(?:0{6,}|9{6,})\d{0,3}$', value)):
        number = float(value)
        short = f'{number:.12g}'
        if number and 'e' not in short.lower() and abs(number - float(short)) <= ulp(number):
            value = short
    displayed = re.sub(r'[\s↑↓]+$', '', normalize('NFKC', value))
    if displayed in {'未见', '未检出', '阴性', '阳性', '弱阳性'}:
        return value
    normalized_unit = normalize('NFKC', unit)
    if ''.join(displayed.casefold().split()).endswith(''.join(normalized_unit.casefold().split())):
        return value
    return f'{value} {unit}'


def _clinical_conclusion(value):
    markers = list(_CLINICAL_CONCLUSION.finditer(value))
    if not markers:
        return ''
    original = value[markers[-1].end():]
    conclusion = re.split(r'(?:\n|(?<=[。；]))\s*(?:医生建议|检查建议|处理意见|建议)\s*[：:]', original, 1)[0]
    return conclusion.strip(' ；;') if len(conclusion) < len(value) else ''


def _clinical_display(row):
    value = re.sub(r'中间结果(?:显示|写明|提示|如下)?\s*[：:]?\s*', '', _value(row))
    status = row.get('templateStatus') or row.get('status')
    if (len(value) > 85 or status in _STATED_NORMAL_IMAGING) and (conclusion := _clinical_conclusion(value)):
        if re.search(r'乳腺|双乳', _text(row.get('name'))):
            measurements = re.search(r'(?:测得|测量)[：:]\s*([^。\n]+)', value)
            if measurements and re.search(r'左乳.*右乳|右乳.*左乳', measurements[1]):
                return measurements[1].strip() + '。' + conclusion
        return conclusion
    if len(value) > 85 and status in _STATED_NORMAL_IMAGING and _CLINICAL_IMAGE.search(_text(row.get('name'))):
        return status
    return re.sub(r'；\s*；+', '；', value)


def _digestive_measurements(group):
    rows = group.get('rows') or ()
    code = group.get('code')
    if code == 'oralGastric':
        scoped = []
        for row in rows:
            value = _text(row.get('value'))
            oral = re.search(r'(?:^|\n)口腔科(?:检室)?\s*\n(.+?)(?=\n体检日期|\Z)', value, re.S)
            scoped.append({**row, 'value': oral[1].strip()} if oral else row)
        return scoped
    if code != 'hepatobiliaryPancreatic':
        return rows
    scoped = []
    for row in rows:
        name, value = _text(row.get('name')), _value(row)
        # A projected atom already identifies its organ. Re-parsing its
        # numeric value as a multi-organ narrative would erase measurements.
        if (_HEPATOBILIARY_ORGANS.search(name) and not _OTHER_ABDOMINAL_ORGANS.search(name)
                or re.fullmatch(r'[<>≤≥]?\s*[+-]?\d+(?:\.\d+)?', _text(row.get('value')).strip())):
            scoped.append(row)
            continue
        if not _CLINICAL_IMAGE.search(name) or not (
            re.search(r'腹部|腹腔', name) or _OTHER_ABDOMINAL_ORGANS.search(value)
        ):
            scoped.append(row)
            continue
        # A combined imaging report's final result may describe a different organ.
        excerpts = []
        for part in re.split(r'[。；;\r\n]+', value):
            sentence = _CLINICAL_CONCLUSION.sub('', part).strip()
            if _HEPATOBILIARY_ORGANS.search(sentence) and not _OTHER_ABDOMINAL_ORGANS.search(sentence):
                excerpts.append(sentence)
        scoped.append({**row, 'value': '。'.join(dict.fromkeys(excerpts)), 'unit': None,
                       'status': None, 'templateStatus': None})
    return scoped


def _is_advice(row):
    """Recorded examination advice: the ``kind`` field, or the legacy status marker of older callbacks."""
    return row.get('kind') == 'advice' or row.get('status') in {'原报告医嘱', '检查建议'}


def _recorded_advice(item):
    quotes = {}
    for row in item.get('clinicalSignals') or ():
        if not _is_advice(row) or not (quote := _text(row.get('value'))):
            continue
        if not any(ref.get('sourceCode') == 'A' and ref.get('recordId') for ref in row.get('sources') or ()):
            continue
        labels = quotes.setdefault(quote, {})
        if label := _text(row.get('name')).partition(' · ')[2]:
            labels[label] = None
    return quotes


def _cluster_anchor(cluster_id):
    if isinstance(cluster_id, str) and cluster_id.startswith('C') and cluster_id[1:].isascii():
        if cluster_id[1:].isdigit():
            return 'as-cluster-' + cluster_id
    return None


def _reference_link(row):
    doi = _text(row.get('doi')).strip()
    if _DOI.fullmatch(doi):
        return _e(f'https://doi.org/{doi}')
    for link in (doi, row.get('url')):
        if clean := _clean_reference_url(link):
            return _e(clean)
    return ''


def _clean_reference_url(link):
    if not isinstance(link, str):
        return ''
    try:
        parsed = urlsplit(link.strip())
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return ''
    if parsed.scheme not in {'http', 'https'} or not host or parsed.username is not None or port not in {None, 80, 443}:
        return ''
    path = parsed.path.rstrip('/')
    if host in {'doi.org', 'dx.doi.org'}:
        doi = path.lstrip('/')
        return f'https://doi.org/{doi}' if _DOI.fullmatch(doi) else ''
    if host == 'vertexaisearch.cloud.google.com' or path.lower().endswith(('/url', '/redirect')):
        return ''
    parameters = parse_qsl(parsed.query, keep_blank_values=True)
    if path.lower() in {'', '/articles', '/pmc', '/pubmed', '/results'} and any(
        key.lower() in {'q', 'query', 'search', 'term'} for key, _ in parameters
    ):
        return ''
    if re.search(r'(?:^|/)search(?:[./]|$)', path.lower()):
        args = dict(parameters)
        if not (
            host == 'www.embase.com'
            and args.get('subaction') == 'viewrecord'
            and re.fullmatch(r'L\d+', args.get('id', ''))
        ):
            return ''
    retained = [
        (key, value)
        for key, value in parameters
        if key.lower() not in _TRACKING_KEYS and not key.lower().startswith(('utm_', '_x_output_type_'))
    ]
    return parsed._replace(query=urlencode(retained)).geturl()


def _numbered_refs(data):
    rows = (data.get('appendices') or {}).get('referenceLiterature') or ()
    return {row['number']: row for row in rows if type(row.get('number')) is int and row['number'] > 0}



# ---------- vocabulary ----------

_ICONS = {
    'focus': '<svg viewBox="0 0 12 12" aria-hidden="true"><circle cx="6" cy="6" r="5" fill="currentColor"/>'
    '<rect x="5.2" y="2.6" width="1.6" height="4.4" rx=".8" fill="#fff"/><circle cx="6" cy="8.9" r=".9" fill="#fff"/>'
    '</svg>',
    'borderline': '<svg viewBox="0 0 12 12" aria-hidden="true"><circle cx="6" cy="6" r="4.6" fill="none" '
    'stroke="currentColor" stroke-width="1.8"/><circle cx="6" cy="6" r="1.6" fill="currentColor"/></svg>',
    'normal': '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M2.2 6.3 4.9 9 9.8 3.4" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    'notTested': '<svg viewBox="0 0 12 12" aria-hidden="true"><rect x="2.4" y="5.2" width="7.2" height="1.6" rx=".8" '
    'fill="currentColor"/></svg>',
    'P0': '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 1 11 10.5H1Z" fill="currentColor"/></svg>',
}
_PRIORITIES = {
    'P0': ('优先医疗排查', 'p0'),
    'P1': ('核心干预', 'p1'),
    'P2': ('协同调理', 'p2'),
    'P3': ('随访巩固', 'p3'),
}
_CLINICAL_SECTIONS = (
    ('cardiometabolic', '3.1 糖脂代谢、胰岛素敏感性与心血管系统'),
    ('organMetabolism', '3.2 肝胆胰消化、肾脏代谢与营养辅酶'),
    ('hematologyImmune', '3.3 血液学、铁代谢、免疫与一碳甲基化'),
    ('endocrineMusculoskeletal', '3.4 内分泌激素、骨骼肌肉与器官影像'),
)
_SECTION_TOPICS = {
    'cardiometabolic': '糖脂代谢与心血管',
    'organMetabolism': '肝胆胰与肾脏',
    'hematologyImmune': '血液与免疫',
    'endocrineMusculoskeletal': '内分泌、骨骼肌肉与器官影像',
}
_OTHER_SYSTEM = ('otherClinical', '其他器官与专项检查')
_STAGES = (
    ('s1', '第一层级', '首月', '临床专科排查与随访', '本次未发现需在首月内完成的专科排查或随访事项。'),
    ('s2', '第二层级', '90 天', '核心代谢与肠道微生态干预', '本次未形成需在 90 天内集中干预的代谢或微生态方向。'),
    ('s3', '第三层级', '持续', '营养储备与长期健康维护', '保持现有健康习惯，按常规周期复查。'),
)
_LAYER_STAGES = {'clinical': 's1', 'routine': 's1', 'core': 's2', 'maintenance': 's3'}
_PRIORITY_STAGES = {'P0': 's1', 'P1': 's2', 'P2': 's3', 'P3': 's3'}
_FINDING_PRIORITIES = {'urgent': 'P0', 'planned': 'P1'}
_LIFESTYLE_DIMENSIONS = (
    ('diet', '饮食与进餐', r'饮食|进餐|正餐|夜宵|甜食|膳食|主食|三餐|零食|含糖'),
    ('drinks', '饮酒与饮品', r'饮酒|喝酒|酒精|饮品|饮水|喝水|咖啡|浓茶'),
    ('sleep', '睡眠', r'睡眠|入睡|作息|熬夜|睡'),
    ('exercise', '运动与久坐', r'运动|久坐|体力活动|锻炼|步行'),
    ('stress', '精神压力与口腔护理', r'压力|情绪|焦虑|口腔|刷牙|牙'),
)
_BACKGROUND_KINDS = {'background', 'history', 'trigger'}
_SCHEDULE = (('首月', 31), ('3 个月', 92), ('6 个月', 183), ('12 个月', 366))
_QA = re.compile(
    r'^\s*(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?\s+)?'
    r'(?P<label>[^；;：:\n]{1,80}?)\s*(?:：|(?<!\d):(?!\d))\s*(?P<answer>.*)$',
    re.S,
)
_LABEL_NOISE = (
    (re.compile(r'（原报告菌群关联评估）|\(原报告菌群关联评估\)'), '（菌群关联评估）'),
    (re.compile(r'原报告|原件|主检'), ''),
)
_MICROBIOME_INDEX_NOTE = '菌群功能评分为基因预测的 0–100 分相对评分，反映代谢潜力，不等同于体内营养素水平。'


def _label(value):
    """A program-supplied label without back-office wording; prose is never rewritten here."""
    text = _text(value)
    for pattern, replacement in _LABEL_NOISE:
        text = pattern.sub(replacement, text)
    return text.strip()


def _prompt(question):
    """A questionnaire prompt as a short, impersonal label."""
    text = _text(question).strip()
    for pattern, label in _LIFESTYLE_PROMPTS:
        if re.fullmatch(pattern, text):
            return label
    text = re.sub(r'您的|您', '', text)
    text = re.sub(r'(?:可多选。?)$', '', text).strip()
    return text.rstrip('？?。').strip()


def _qa_pairs(value):
    """(label, answer) pairs of "题目：答案；题目：答案"; a piece without a label continues the previous answer."""
    text = _text(value).strip()
    if not text:
        return []
    pairs = []
    for piece in re.split(r'[；;\n]', text):
        piece = piece.strip()
        if not piece:
            continue
        match = _QA.match(piece)
        if match and match['answer'].strip():
            label = match['label'].strip()
            pairs.append(['' if label == '体检生活方式记录原文' else _prompt(label), match['answer'].strip()])
        elif pairs:
            pairs[-1][1] += '；' + piece
        else:
            pairs.append(['', piece])
    return [tuple(pair) for pair in pairs]


def _qa_html(value):
    rows = []
    for label, answer in _qa_pairs(value):
        if label:
            rows.append(f'<span class="qa">{_e(label)}：{_e(answer)}</span>')
        elif answer:
            rows.append(f'<span class="qa">{_e(answer)}</span>')
    return ''.join(rows)


def _fmt(number):
    if number is None:
        return ''
    if number == 0:
        return '0'
    magnitude = abs(number)
    if magnitude >= 1000:
        text = f'{number:.0f}'
    elif magnitude >= 100:
        text = f'{number:.1f}'
    elif magnitude >= 1:
        text = f'{number:.2f}'
    else:
        text = f'{number:.{max(2, 2 - math.floor(math.log10(magnitude)))}f}'
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _unit(unit):
    return re.sub(r'(?<![A-Za-z])(kg/m|cm|m)2(?![0-9A-Za-z])', r'\1²', _text(unit))


def _level_class(level):
    return level if level in charts.LEVEL_LABELS else 'none'


def _level_chip(level, label=None):
    if level not in charts.LEVEL_LABELS:
        return ''
    text = label or charts.LEVEL_LABELS[level]
    return f'<span class="as-lv lv-{level}">{_ICONS[level]}{_e(text)}</span>'


def _priority_chip(priority):
    if priority not in _PRIORITIES:
        return ''
    label, css = _PRIORITIES[priority]
    icon = _ICONS['P0'] if priority == 'P0' else ''
    return f'<span class="as-pri pri-{css}">{icon}{_e(priority)} {_e(label)}</span>'


def _arrow(direction):
    if direction == 'up':
        return (
            '<svg class="as-dir" viewBox="0 0 8 10" role="img" aria-label="偏高">'
            '<path d="M4 0 8 5.2H5.1V10H2.9V5.2H0Z"/></svg>'
        )
    if direction == 'down':
        return (
            '<svg class="as-dir" viewBox="0 0 8 10" role="img" aria-label="偏低">'
            '<path d="M4 10 8 4.8H5.1V0H2.9V4.8H0Z"/></svg>'
        )
    return ''


def _note(text):
    return f'<p class="as-missing">{_e(text)}</p>'


def _subsection(title, content, *, anchor=None):
    anchor_attr = f' id="{_e(anchor)}"' if anchor else ''
    return f'<h3{anchor_attr}>{_e(title)}</h3>{content}' if content else ''


def _days_between(first, second):
    try:
        return abs((date.fromisoformat(str(second)[:10]) - date.fromisoformat(str(first)[:10])).days)
    except ValueError:
        return None


def _shared_date(rows):
    dates = {_text(row.get('observedAt'))[:10] for row in rows}
    return next(iter(dates)) if len(dates) == 1 and '' not in dates else ''


def _text_width(text, size):
    return sum(size if ord(char) > 0x2E80 else size * 0.58 for char in _text(text))


def _wrap(text, size, width, lines=2):
    """Lines of at most ``width`` user units at ``size``; CJK glyphs count as one em."""
    result, current, used = [], '', 0.0
    for char in _text(text):
        advance = size if ord(char) > 0x2E80 else size * 0.58
        if used + advance > width and current:
            result.append(current)
            current, used = '', 0.0
            if len(result) == lines:
                break
        current += char
        used += advance
    else:
        if current:
            result.append(current)
        return result
    result[-1] = result[-1][:-1] + '…'
    return result


# ---------- scales (inline SVG; numbers are always printed beside them) ----------

_SW = 260.0
_X0 = 10.0
_X1 = _SW - 10.0


def _x(value, low, high):
    return _X0 + (value - low) / ((high - low) or 1.0) * (_X1 - _X0)


def _range_svg(number, low, high, kind, level, label, target=None):
    """Clinical reference band, optional target band and the measured point."""
    target = target if isinstance(target, dict) else {}
    t_low, t_high = target.get('low'), target.get('high')
    values = [value for value in (number, low, high, t_low, t_high) if isinstance(value, (int, float))]
    if kind == 'range':
        span = (high - low) or abs(high) or 1.0
        lo, hi = low - 0.55 * span, high + 0.55 * span
        band = (low, high)
    elif kind == 'upper':
        span = abs(high) or 1.0
        lo, hi = (0.0 if high > 0 else high - span), high + 0.6 * span
        band = (None, high)
    else:
        span = abs(low) or 1.0
        lo, hi = low - 0.6 * span, low + 0.9 * span
        band = (low, None)
    lo, hi = min(lo, *values), max(hi, *values)
    anchor_low = low if low is not None else high
    anchor_high = high if high is not None else low
    overflow = ''
    if number is not None and number > anchor_high + 4 * span:
        hi, overflow = anchor_high + 3 * span, 'right'
    elif number is not None and number < anchor_low - 4 * span:
        lo, overflow = anchor_low - 3 * span, 'left'
    if all(value >= 0 for value in values):
        lo = max(lo, 0.0)
    pad = (hi - lo) * 0.04
    lo, hi = (lo if lo == 0 else lo - pad), hi + pad
    b0 = _x(band[0], lo, hi) if band[0] is not None else _X0
    b1 = _x(band[1], lo, hi) if band[1] is not None else _X1
    parts = [
        f'<svg viewBox="0 0 {_SW:.0f} 32" role="img" aria-label="{_e(label)}">',
        f'<line class="trk" x1="{_X0}" y1="13" x2="{_X1}" y2="13"/>',
        f'<rect class="band" x="{b0:.1f}" y="7" width="{max(b1 - b0, 2):.1f}" height="12" rx="3"/>',
    ]
    if t_low is not None or t_high is not None:
        g0 = _x(max(t_low, lo), lo, hi) if t_low is not None else _X0
        g1 = _x(min(t_high, hi), lo, hi) if t_high is not None else _X1
        parts.append(f'<rect class="tgt" x="{g0:.1f}" y="21.5" width="{max(g1 - g0, 2):.1f}" height="3.2" rx="1.6"/>')
    ticks = [(b0, band[0], 'end' if b1 - b0 < 26 else 'middle'), (b1, band[1], 'start' if b1 - b0 < 26 else 'middle')]
    for position, value, anchor in ticks:
        if value is not None:
            parts.append(f'<text class="tk" x="{position:.1f}" y="31" text-anchor="{anchor}">{_e(_fmt(value))}</text>')
    if number is not None:
        cx = min(max(_x(number, lo, hi), _X0), _X1)
        parts.append(f'<circle class="ring" cx="{cx:.1f}" cy="13" r="7.2"/>')
        parts.append(f'<circle class="dot lv-{_level_class(level)}" cx="{cx:.1f}" cy="13" r="6.1"/>')
        if overflow == 'right':
            parts.append(f'<path class="over" d="M{_X1 + 4:.1f} 9.5 {_X1 + 8:.1f} 13 {_X1 + 4:.1f} 16.5Z"/>')
        elif overflow == 'left':
            parts.append(f'<path class="over" d="M{_X0 - 4:.1f} 9.5 {_X0 - 8:.1f} 13 {_X0 - 4:.1f} 16.5Z"/>')
    parts.append('</svg>')
    return ''.join(parts)


def _band_class(label):
    """Normal bands are green; low bands blue and high bands warm, deeper at the extremes."""
    text = _text(label)
    if charts.is_normal_label(text):
        return 'bn'
    if re.search(r'极低|严重偏低', text):
        return 'blx'
    if re.search(r'极高|严重偏高', text):
        return 'bhx'
    if re.search(r'低|少|弱', text):
        return 'blo'
    if re.search(r'高|多|强', text):
        return 'bhi'
    return 'bo'


def _bands_svg(number, bands, level, label, *, domain=None):
    """Grade bands of the source report as the background, the measured point on top."""
    finite = [value for band in bands for value in (band.get('low'), band.get('high')) if value is not None]
    if domain:
        lo, hi = domain
    else:
        lo, hi = min(finite), max(finite)
        span = (hi - lo) or 1.0
        if any(band.get('low') is None for band in bands):
            lo -= 0.25 * span
        if any(band.get('high') is None for band in bands):
            hi += 0.25 * span
        if number is not None:
            lo, hi = min(lo, number), max(hi, number)
    parts = [f'<svg viewBox="0 0 {_SW:.0f} 32" role="img" aria-label="{_e(label)}">']
    edges = set()
    for band in bands:
        x0 = _x(band['low'], lo, hi) if band.get('low') is not None else _X0
        x1 = _x(band['high'], lo, hi) if band.get('high') is not None else _X1
        x0, x1 = max(x0, _X0), min(x1, _X1)
        if x1 - x0 < 0.5:
            continue
        parts.append(
            f'<rect class="{_band_class(band["label"])}" x="{x0 + 0.5:.1f}" y="6" width="{max(x1 - x0 - 1, 1):.1f}" '
            'height="14" rx="2"/>'
        )
        if len(band['label']) * 10.2 + 6 < x1 - x0:
            middle = (x0 + x1) / 2
            parts.append(f'<text class="bt" x="{middle:.1f}" y="16.6" text-anchor="middle">{_e(band["label"])}</text>')
        for value in (band.get('low'), band.get('high')):
            if value is not None and lo < value < hi:
                edges.add(value)
    for value in sorted(edges):
        x = _x(value, lo, hi)
        parts.append(f'<text class="tk" x="{x:.1f}" y="31" text-anchor="middle">{_e(_fmt(value))}</text>')
    if number is not None:
        cx = min(max(_x(number, lo, hi), _X0), _X1)
        parts.append(f'<circle class="ring" cx="{cx:.1f}" cy="13" r="7.2"/>')
        parts.append(f'<circle class="dot lv-{_level_class(level)}" cx="{cx:.1f}" cy="13" r="6.1"/>')
    parts.append('</svg>')
    return ''.join(parts)


def _axis_svg(number, lo, hi, ticks, level, label, *, marker=None):
    """A plain axis with labelled ticks, an optional reference marker and the measured point."""
    parts = [
        f'<svg viewBox="0 0 {_SW:.0f} 32" role="img" aria-label="{_e(label)}">',
        f'<line class="trk" x1="{_X0}" y1="13" x2="{_X1}" y2="13"/>',
    ]
    for value, text in ticks:
        x = _x(value, lo, hi)
        parts.append(f'<line class="tkl" x1="{x:.1f}" y1="9" x2="{x:.1f}" y2="17"/>')
        parts.append(f'<text class="tk" x="{x:.1f}" y="31" text-anchor="middle">{_e(text)}</text>')
    if marker is not None:
        x = _x(marker, lo, hi)
        parts.append(f'<rect class="tgt" x="{x - 1:.1f}" y="5" width="2" height="16" rx="1"/>')
    if number is not None:
        cx = min(max(_x(number, lo, hi), _X0), _X1)
        parts.append(f'<circle class="ring" cx="{cx:.1f}" cy="13" r="7.2"/>')
        parts.append(f'<circle class="dot lv-{_level_class(level)}" cx="{cx:.1f}" cy="13" r="6.1"/>')
    parts.append('</svg>')
    return ''.join(parts)


def _score_svg(score, bands, level, label):
    """A 0–100 score over its grade bands (no text; the legend line names the bands)."""
    width = 200.0
    parts = [f'<svg viewBox="0 0 {width:.0f} 16" role="img" aria-label="{_e(label)}">']
    for band in bands:
        low = 0.0 if band.get('low') is None else max(band['low'], 0.0)
        high = 100.0 if band.get('high') is None else min(band['high'], 100.0)
        x0, x1 = 4 + low / 100 * (width - 8), 4 + high / 100 * (width - 8)
        if x1 - x0 >= 1:
            parts.append(
                f'<rect class="{_band_class(band["label"])}" x="{x0 + 0.5:.1f}" y="3" width="{x1 - x0 - 1:.1f}" '
                'height="10" rx="2"/>'
            )
    cx = 4 + min(max(score, 0.0), 100.0) / 100 * (width - 8)
    parts.append(f'<circle class="ring" cx="{cx:.1f}" cy="8" r="5.6"/>')
    parts.append(f'<circle class="dot lv-{_level_class(level)}" cx="{cx:.1f}" cy="8" r="4.6"/>')
    parts.append('</svg>')
    return ''.join(parts)


def _band_key(bands):
    return ''.join(
        f'<span><i class="{_band_class(band["label"])}"></i>{_e(band["label"])} {_e(_band_text(band))}</span>'
        for band in bands
    )

_LOG_LOW, _LOG_HIGH = -4.0, 2.0  # 0.0001 % to 100 %, one shared axis for every abundance row


def _log_x(percent):
    value = min(max(percent, 10**_LOG_LOW), 10**_LOG_HIGH)
    return _x(math.log10(value), _LOG_LOW, _LOG_HIGH)


def _abundance_svg(abundance, low, high, level, label):
    """Relative abundance on a log axis against the reported reference band; 0 is drawn hollow at the origin."""
    parts = [
        f'<svg viewBox="0 0 {_SW:.0f} 26" role="img" aria-label="{_e(label)}">',
        f'<line class="trk" x1="{_X0}" y1="13" x2="{_X1}" y2="13"/>',
    ]
    for exponent in range(int(_LOG_LOW), int(_LOG_HIGH) + 1):
        x = _x(exponent, _LOG_LOW, _LOG_HIGH)
        parts.append(f'<line class="tkl" x1="{x:.1f}" y1="10" x2="{x:.1f}" y2="16"/>')
    if high is not None:
        b0 = _log_x(low) if low else _X0
        b1 = _log_x(high)
        parts.append(f'<rect class="band" x="{b0:.1f}" y="7" width="{max(b1 - b0, 2):.1f}" height="12" rx="3"/>')
    if abundance is not None:
        if abundance <= 0:
            parts.append(f'<circle class="hollow" cx="{_X0:.1f}" cy="13" r="5"/>')
        else:
            cx = _log_x(abundance)
            parts.append(f'<circle class="ring" cx="{cx:.1f}" cy="13" r="7.2"/>')
            parts.append(f'<circle class="dot lv-{_level_class(level)}" cx="{cx:.1f}" cy="13" r="6.1"/>')
    parts.append('</svg>')
    return ''.join(parts)


def _abundance_axis():
    labels = ''.join(
        f'<text class="tk" x="{_x(exponent, _LOG_LOW, _LOG_HIGH):.1f}" y="10" text-anchor="middle">'
        f'{_e(_fmt(10.0**exponent))}</text>'
        for exponent in range(int(_LOG_LOW), int(_LOG_HIGH) + 1)
    )
    svg = f'<svg viewBox="0 0 {_SW:.0f} 13" aria-hidden="true">{labels}</svg>'
    return f'<div class="as-axis"><div></div><div></div><div>{svg}</div><div>相对丰度（%，对数刻度）</div></div>'


def _trend_svg(points, label):
    values = [(row, charts.parse_number(row.get('value'))) for row in points]
    values = [(row, number) for row, number in values if number is not None]
    if len(values) < 2:
        return ''
    numbers = [number for _, number in values]
    lo, hi = min(numbers), max(numbers)
    span = (hi - lo) or abs(hi) or 1.0
    lo, hi = lo - 0.2 * span, hi + 0.2 * span
    width, height = 240.0, 64.0
    step = (width - 40) / (len(values) - 1)
    coords = [(20 + index * step, 44 - (number - lo) / (hi - lo) * 30) for index, (_, number) in enumerate(values)]
    path = ' '.join(f'{x:.1f},{y:.1f}' for x, y in coords)
    parts = [f'<svg viewBox="0 0 {width:.0f} {height:.0f}" role="img" aria-label="{_e(label)}">',
             f'<polyline class="line" points="{path}"/>']
    for index, ((row, number), (x, y)) in enumerate(zip(values, coords, strict=True)):
        parts.append(f'<circle class="pt" cx="{x:.1f}" cy="{y:.1f}" r="3.4"/>')
        if index in {0, len(values) - 1}:
            anchor = 'start' if index == 0 else 'end'
            parts.append(
                f'<text class="tk" x="{x:.1f}" y="{y - 7:.1f}" text-anchor="{anchor}">{_e(_fmt(number))}</text>'
            )
            parts.append(
                f'<text class="tk" x="{x:.1f}" y="60" text-anchor="{anchor}">{_e(_text(row.get("date"))[:10])}</text>'
            )
    parts.append('</svg>')
    return ''.join(parts)


def _graph_svg(hub, hub_lines, nodes):
    """Centre (key indicators) to cluster nodes; names wrap to two lines, text is 11.5–15 px."""
    width, gap, node_x = 680.0, 10.0, 300.0
    text_width = width - node_x - 6 - 56
    laid = []
    for identifier, name, priority, anchors in nodes:
        lines = _wrap(name, 13, text_width, lines=2)
        sub = f'{priority} {_PRIORITIES[priority][0]}' if priority in _PRIORITIES else ''
        for anchor in anchors:  # an anchor that does not fit is left out, never cut
            candidate = f'{sub} · {anchor}' if sub else anchor
            if _text_width(candidate, 11.5) <= text_width:
                sub = candidate
        laid.append((identifier, lines, priority, [sub] if sub else [], 22 + 18 * len(lines) + 16))
    total = sum(item[4] for item in laid) + gap * (len(laid) - 1)
    hub_h = 40 + 20 * len(hub_lines)
    height = max(total + 16, hub_h + 24)
    hub_y = (height - hub_h) / 2
    parts = [f'<svg viewBox="0 0 {width:.0f} {height:.0f}" role="img" aria-label="{_e(hub)}与失衡集群关系">']
    y = (height - total) / 2
    boxes = []
    for item in laid:
        boxes.append((y, item))
        cy = y + item[4] / 2
        parts.append(
            f'<path class="edge" d="M230 {height / 2:.1f} C265 {height / 2:.1f} {node_x - 35:.1f} {cy:.1f} '
            f'{node_x:.1f} {cy:.1f}"/>'
        )
        y += item[4] + gap
    parts.append(f'<rect class="hub" x="8" y="{hub_y:.1f}" width="222" height="{hub_h:.1f}" rx="8"/>')
    parts.append(f'<text class="hub-t" x="22" y="{hub_y + 26:.1f}">{_e(_wrap(hub, 15, 196, lines=1)[0])}</text>')
    for index, line in enumerate(hub_lines):
        text = _wrap(line, 12, 196, lines=1)[0]
        parts.append(f'<text class="hub-s" x="22" y="{hub_y + 48 + index * 20:.1f}">{_e(text)}</text>')
    for top, (identifier, lines, priority, sub, box_h) in boxes:
        css = _PRIORITIES.get(priority, ('', 'p3'))[1]
        parts.append(
            f'<rect class="node pri-{css}" x="{node_x:.1f}" y="{top:.1f}" width="{width - node_x - 6:.0f}" '
            f'height="{box_h:.0f}" rx="7"/>'
        )
        parts.append(f'<rect class="badge pri-{css}" x="{node_x + 10:.1f}" y="{top + 9:.1f}" width="32" height="19"/>')
        parts.append(f'<text class="bid" x="{node_x + 26:.1f}" y="{top + 23:.1f}" text-anchor="middle">'
                     f'{_e(identifier)}</text>')
        for index, line in enumerate(lines):
            parts.append(f'<text class="nt" x="{node_x + 50:.1f}" y="{top + 23 + index * 18:.1f}">{_e(line)}</text>')
        if sub:
            parts.append(f'<text class="ns" x="{node_x + 50:.1f}" y="{top + 23 + len(lines) * 18 + 2:.1f}">'
                         f'{_e(sub[0])}</text>')
    parts.append('</svg>')
    return ''.join(parts)


def _legend():
    def chip_row():
        return ''.join(_level_chip(level) for level in charts.LEVELS)

    sample = (
        '<svg viewBox="0 0 92 18" aria-hidden="true"><line class="trk" x1="2" y1="8" x2="90" y2="8"/>'
        '<rect class="band" x="24" y="3" width="40" height="10" rx="3"/>'
        '<rect class="tgt" x="34" y="14.5" width="22" height="3" rx="1.5"/>'
        '<circle class="ring" cx="74" cy="8" r="5.9"/><circle class="dot lv-focus" cx="74" cy="8" r="4.8"/></svg>'
    )
    return (
        '<div class="as-legend" aria-label="阅读图例">'
        f'<b>状态分档</b><div>{chip_row()}</div>'
        f'<b>参考范围条</b><div><span class="sw">{sample}</span>'
        '<span>绿色区段为临床参考区间，下方深色细条为有出处的目标区间，圆点为本次实测值并按状态着色</span></div>'
        '<b>未分级</b><div><span class="as-tag lv-none"><i></i>空心圆点</span>'
        '<span>没有可比的参考区间或分档，只列出原始数值</span></div>'
        f'<b>优先级</b><div>{"".join(_priority_chip(key) for key in ("P0", "P1", "P2"))}</div>'
        '</div>'
    )


def _schedule_marks(days):
    """Column -> first/repeat mark of an interval within twelve months."""
    if not isinstance(days, (int, float)) or days <= 0:
        return {}
    marks = {}
    moment = float(days)
    while moment <= 366:
        column = next((index for index, (_, limit) in enumerate(_SCHEDULE) if moment <= limit), None)
        if column is None:
            break
        marks.setdefault(column, 're' if marks else 'first')
        moment += days
    return marks


def _cited_numbers(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, list) and (key == 'evidenceRefs' or key.endswith('EvidenceRefs')):
                yield from (number for number in value if type(number) is int)
            else:
                yield from _cited_numbers(value)
    elif isinstance(node, list):
        for item in node:
            yield from _cited_numbers(item)


def _anchor_tag(anchor, level):
    """A system anchor (spec 2.7 ``anchors[]``) as a tag."""
    value = f' <b>{_e(anchor["value"])}</b>' if anchor.get('value') else ''
    unit = f'<small>{_e(_unit(anchor["unit"]))}</small>' if anchor.get('unit') else ''
    css = _level_class(anchor.get('level') or level)
    return (
        f'<span class="as-tag lv-{css}"><i></i>{_e(_label(anchor.get("name")))}{value}{unit}'
        f'{_arrow(anchor.get("direction"))}</span>'
    )


def _advice_quotes(item):
    return list(_recorded_advice(item))


class _AssessmentRenderer:
    def __init__(self, data, presentation, *, status=None, partial_reasons=()):
        # Completeness travels in the callback status; the page shows no banner.
        self.data = presentation
        # The appendix lists the works this report cites, as the delivered callback does.
        cited = set(_cited_numbers(presentation))
        self.references = {number: row for number, row in _numbered_refs(data).items() if number in cited}
        self.core_clusters = list(presentation.get('clusters') or ())
        self.cluster_ids = {item.get('id') for item in self.core_clusters}
        self.overview_data = presentation.get('overview') or {}
        self.clinical_data = presentation.get('clinical') or {}
        # Older callbacks carry no chart fields; the same parser fills them on a copy.
        self.microbiome_data = annotate_microbiome_chart_fields(deepcopy(presentation.get('microbiome') or {}))
        self.conclusion_data = presentation.get('conclusion') or {}

    # ----- shared pieces -----

    def refs(self, node):
        numbers = {
            number: None
            for number in (node or {}).get('evidenceRefs') or ()
            if type(number) is int and number in self.references
        }
        links = ''
        for number in numbers:
            row = self.references[number]
            title = ' · '.join(_text(value) for value in (row.get('title'), row.get('year')) if value)
            title_attr = f' title="{_e(title)}"' if title else ''
            links += f'<a href="#ref-{number}"{title_attr}>[{number}]</a>'
        return f'<span class="cite">{links}</span>' if links else ''

    def paragraph(self, value, node=None, *, css=''):
        parts = [part.strip() for part in _text(value).split('\n\n') if part.strip()]
        if not parts:
            return ''
        tail = self.refs(node) if node is not None else ''
        klass = f' class="{css}"' if css else ''
        return ''.join(
            f'<p{klass}>{_e(part)}{tail if index == len(parts) - 1 else ""}</p>' for index, part in enumerate(parts)
        )

    def cluster_link(self, cluster_id, label):
        anchor = _cluster_anchor(cluster_id)
        if anchor and cluster_id in self.cluster_ids:
            return f'<a href="#{anchor}">{_e(label)}</a>'
        return _e(label)

    def tag(self, row, *, microbiome=False, name=None):
        """One measured value as a tag: name, value, unit, direction; the dot carries the level."""
        fields = charts.microbiome_chart_fields(row) if microbiome else charts.clinical_chart_fields(row)
        level = fields.get('level')
        if name is None:
            name = charts.display_name(clinical_observation_display_name(row) or row.get('name'))
        label = name
        label = _label(label)
        number = charts.parse_number(row.get('value'), row.get('unit'))
        value = ''
        if number is not None:
            value = f'<b>{_e(_text(row.get("value")).strip())}</b>'
            if row.get('unit'):
                value += f'<small>{_e(_unit(row["unit"]))}</small>'
        else:
            shown = _clinical_display(row) if not microbiome else _value(row)
            if shown and shown != label and len(shown) <= 18 and shown not in label:
                value = f'<b>{_e(shown)}</b>'
        if not label and not value:
            return ''
        return (
            f'<span class="as-tag lv-{_level_class(level)}"><i></i>{_e(label)}{" " if value else ""}{value}'
            f'{_arrow(fields.get("direction"))}</span>'
        )

    def tags(self, rows, *, microbiome=False):
        items = [self.tag(row, microbiome=microbiome) for row in rows or ()]
        unique = list(dict.fromkeys(item for item in items if item))
        return f'<div class="as-tags">{"".join(unique)}</div>' if unique else ''

    def measure_row(self, row, *, name=None, date_shown=False):
        """A clinical row: name | value + level | reference bar | reference and target text."""
        fields = charts.clinical_chart_fields(row)
        level, number = fields['level'], fields['valueNumber']
        title = _label(name or charts.display_name(row.get('name')))
        when = f'<small>{_e(row["observedAt"])}</small>' if date_shown and row.get('observedAt') else ''
        if row.get('sourceKind') == 'derived':
            when += '<small>计算值</small>'
        chip = _level_chip(level) if level in {charts.FOCUS, charts.BORDERLINE} else ''
        if number is None:
            text = _clinical_display(row)
            if title and text.startswith(title):
                text = text[len(title) :].lstrip(' ：:')
            return (
                f'<div class="as-mrow"><div class="nm">{_e(title)}{when}</div><div class="val">{chip}</div>'
                f'<div class="txt">{_e(text) or _e(_text(row.get("templateStatus") or row.get("status")))}</div></div>'
            )
        value = _text(row.get('value')).strip()
        shown = f'<b>{_e(value)}</b> {_e(_unit(row.get("unit")))}{_arrow(fields["direction"])}'
        reference_text = _text(row.get('referenceRange')).strip()
        target = fields.get('target') if isinstance(fields.get('target'), dict) else None
        low, high, kind = fields['referenceLow'], fields['referenceHigh'], fields['referenceKind']
        label = f'{title} {_text(row.get("value"))}{_text(row.get("unit"))}'
        if kind in {'range', 'upper', 'lower'}:
            chart = _range_svg(number, low, high, kind, level, label + f'，参考 {reference_text}', target)
        elif target and (target.get('low') is not None or target.get('high') is not None):
            t_kind = 'range' if target.get('low') is not None and target.get('high') is not None else (
                'upper' if target.get('high') is not None else 'lower')
            chart = _range_svg(number, target.get('low'), target.get('high'), t_kind, level, label, None)
        else:
            chart = '<span class="gap">无参考区间</span>'
        notes = []
        if reference_text:
            notes.append(f'参考 {_e(reference_text)}')
        status = charts.status_text(row)
        if not reference_text and status and not charts.is_normal_label(status) and len(status) <= 10:
            notes.append(f'分级 {_e(status)}')
        if target:
            target_text = _text(target.get('text')) or '–'.join(
                _fmt(value) for value in (target.get('low'), target.get('high')) if value is not None
            )
            notes.append(f'目标 {_e(target_text)}{self.refs(target)}')
        return (
            f'<div class="as-mrow"><div class="nm">{_e(title)}{when}</div><div class="val">{shown}{chip}</div>'
            f'<div class="ch">{chart}</div><div class="ref">{"<br>".join(notes)}</div></div>'
        )

    def normal_line(self, rows, label='参考范围内'):
        items = []
        for row in rows:
            name = _label(charts.display_name(row.get('name')))
            value = _value(row) if charts.parse_number(row.get('value'), row.get('unit')) is not None else ''
            if not value:
                shown = _clinical_display(row)
                value = shown if shown and shown != name and len(shown) <= 16 else ''
            items.append(f'{_e(name)}{" " if value else ""}<span class="num">{_e(_unit(value))}</span>')
        if not items:
            return ''
        icon = _ICONS['normal'].replace('aria-hidden="true"', 'aria-hidden="true" style="color:var(--normal)"')
        return f'<div class="as-normal">{icon}<span><b>{_e(label)}</b>　{"；".join(items)}</span></div>'

    def panel(self, title, rows, node=None, *, summary=None, extra=''):
        """A measured group: head with level, abnormal rows drawn, normal rows on one line, then the reading."""
        rows = [row for row in rows or () if row.get('name')]
        levels = [charts.clinical_chart_fields(row)['level'] for row in rows]
        abnormal = [row for row, level in zip(rows, levels, strict=True) if level in {charts.FOCUS, charts.BORDERLINE}]
        unknown = [row for row, level in zip(rows, levels, strict=True) if level is None]
        normal = [row for row, level in zip(rows, levels, strict=True) if level == charts.NORMAL]
        shared = _shared_date(rows)
        numeric = [row for row in unknown if charts.parse_number(row.get('value'), row.get('unit')) is not None]
        drawn = [*abnormal, *numeric]
        listed = [row for row in unknown if row not in drawn]
        body = ''.join(self.measure_row(row, date_shown=not shared) for row in drawn)
        body = f'<div class="as-mrows">{body}</div>' if body else ''
        body += self.normal_line(normal)
        body += self.normal_line(listed, '其他记录')
        reading = self.paragraph(summary, node) if summary else ''
        if not body and not reading:
            return ''
        level = charts.worst_level(levels)
        date_html = f'<small>{_e(shared)}</small>' if shared else ''
        return (
            f'<div class="as-panel"><div class="as-panel-head"><h4>{_e(_label(title))}</h4>{date_html}'
            f'{_level_chip(level)}</div>{body}{extra}'
            f'{f"<div class=as-interp>{reading}</div>" if reading else ""}</div>'
        )

    # ----- front matter -----

    def preface(self):
        overview = self.overview_data
        intro = self.paragraph(overview.get('preface'))
        guide = ''.join(
            f'<li><strong>{_e(row.get("name"))}：</strong>{_e(row.get("text"))}</li>'
            for row in overview.get('readingGuide') or ()
            if row.get('text')
        )
        guide = f'<ol class="as-small">{guide}</ol>' if guide else ''
        return intro + guide + _legend()

    # ----- chapter one -----

    def body_measurements(self):
        clinical = self.clinical_data
        groups = clinical.get('bodyGroups') or ()
        if not groups and clinical.get('bodyMetrics'):
            groups = [{'name': '体格测量', 'rows': clinical['bodyMetrics']}]
        blocks = []
        for group in groups:
            rows = [row for row in group.get('rows') or () if row.get('name')]
            if not rows:
                continue
            shared = _shared_date(rows)
            measured = ''.join(self.measure_row(row, date_shown=not shared) for row in rows)
            level = charts.worst_level(charts.clinical_chart_fields(row)['level'] for row in rows)
            date_html = f'<small>{_e(shared)}</small>' if shared else ''
            reading = self.paragraph(group.get('summary'), group)
            blocks.append(
                f'<div class="as-panel"><div class="as-panel-head"><h4>{_e(_label(group.get("name")))}</h4>'
                f'{date_html}{_level_chip(level)}</div><div class="as-mrows">{measured}</div>'
                f'{f"<div class=as-interp>{reading}</div>" if reading else ""}</div>'
            )
        lead = self.paragraph(
            self.overview_data.get('bodySummary'), {'evidenceRefs': self.overview_data.get('bodySummaryEvidenceRefs')},
            css='as-lead',
        )
        if not blocks:
            return lead or _note('本次资料未包含体格测量。')
        return lead + ''.join(blocks)

    def systems(self):
        provided = {row.get('code'): row for row in self.overview_data.get('systems') or ()}
        cards = []
        definitions = list(_SYSTEMS)
        other_rows = [
            row for group in self.clinical_data.get('clinicalGroups') or () if group.get('code') == _OTHER_SYSTEM[0]
            for row in group.get('rows') or ()
        ]
        if other_rows:
            definitions.append((_OTHER_SYSTEM[0], _OTHER_SYSTEM[1], (_OTHER_SYSTEM[0],)))
        for code, name, group_codes in definitions:
            row = provided.get(code) or {}
            fields = {key: row.get(key) for key in ('level', 'anchors', 'moreCount')}
            if fields['level'] is None and fields['anchors'] is None:
                atoms = charts.system_atoms(code, group_codes, self.clinical_data, self.microbiome_data)
                fields = charts.system_chart_fields(atoms, row.get('rows') or (), microbiome=code == 'microbiome')
            level = fields.get('level') or (charts.NOT_TESTED if not row else None)
            if code == _OTHER_SYSTEM[0] and level == charts.NORMAL:
                continue
            anchors = ''.join(_anchor_tag(anchor, level) for anchor in fields.get('anchors') or ())
            more = fields.get('moreCount') or 0
            if more:
                where = '第四章' if code == 'microbiome' else '第三章'
                anchors += f'<span class="as-more">另有 {more} 项，见{where}</span>'
            if level == charts.NOT_TESTED:
                body = '<p>本次资料未包含该系统的检查项目。</p>'
            else:
                verdict = _text(row.get('summary')).split('\n\n')[0].strip()
                body = (f'<div class="as-tags">{anchors}</div>' if anchors else
                        '<p>已检项目均在参考范围内。</p>' if level == charts.NORMAL else '')
                if verdict:
                    body += f'<p>{_e(verdict)}{self.refs(row)}</p>'
            cards.append(
                f'<article class="as-sys lv-{_level_class(level)}"><header><h4>{_e(row.get("name") or name)}</h4>'
                f'{_level_chip(level) or _level_chip(charts.NOT_TESTED, "未分级")}</header>'
                f'<div class="bd">{body}</div></article>'
            )
        summary = self.paragraph(self.overview_data.get('summary'), self.overview_data, css='as-lead')
        return summary + f'<div class="as-systems">{"".join(cards)}</div>'

    def priority_cards(self):
        findings = list(self.overview_data.get('priorityFindings') or ())
        if any('items' in row for row in findings):
            return self.structured_priorities(findings)
        return self.legacy_priorities(findings)

    def structured_priorities(self, findings):
        """1.3 in three parts (``priorityFindings[code=urgent|routine|gaps].items[]``)."""
        sections = {row.get('code'): row for row in findings}
        urgent = sections.get('urgent') or {}
        items = urgent.get('items') or []
        html = '<h4 class="as-sub">优先排查</h4>'
        if items:
            cards = ''.join(self.item_card(item) for item in items)
            html += self.paragraph(urgent.get('text'), urgent) + f'<div class="as-actions">{cards}</div>'
        else:
            html += _note(_text(urgent.get('text')) or '本次未发现需优先就医排查的结果。')
        routine = sections.get('routine') or {}
        items = routine.get('items') or []
        html += '<h4 class="as-sub">常规专科随访</h4>'
        if items:
            scheduled = [item for item in items if isinstance((item.get('timeframe') or {}).get('days'), (int, float))]
            listed = [item for item in items if item not in scheduled]
            html += self.paragraph(routine.get('text'), routine)
            html += self.schedule(scheduled) if scheduled else ''
            html += self.item_list(listed)
        else:
            html += _note(_text(routine.get('text')) or '本次未发现需定期专科复查的发现。')
        gaps = (sections.get('gaps') or {}).get('items') or []
        if gaps:
            html += '<h4 class="as-sub">检测盲区</h4>' + self.paragraph((sections.get('gaps') or {}).get('text'))
            html += self.item_list(gaps, reason=True)
        return html

    def item_card(self, item):
        timeframe = (item.get('timeframe') or {}).get('text')
        title = _text(item.get('name') or item.get('title'))
        # The card line is composed from the drawn fields; it is shown only when they are absent.
        text = None if (timeframe or item.get('department') or item.get('checks')) else item.get('text')
        card = self.action_card(
            title, item.get('priority') or 'P0', timeframe=timeframe, department=item.get('department'),
            checks=item.get('checks'), basis=item.get('basisRows'), text=text,
        )
        if cluster := item.get('clusterId'):
            card = card.replace('</h4></header>', f'</h4>{self.cluster_ref(cluster)}</header>', 1)
        return card

    def cluster_ref(self, cluster_id):
        anchor = _cluster_anchor(cluster_id)
        if anchor and cluster_id in self.cluster_ids:
            return f'<a class="as-more" href="#{anchor}">见集群 {_e(cluster_id)}</a>'
        return ''

    def item_list(self, items, *, reason=False):
        rows = []
        for item in items:
            title = _text(item.get('name') or item.get('title') or item.get('test'))
            detail = _text(item.get('reason')) if reason and item.get('reason') else _text(item.get('text'))
            if detail.startswith(title):
                detail = detail[len(title):].lstrip('：: ')
            meta = ' '.join(
                f'<span class="as-when">{_e(value)}</span>' for value in (
                    (item.get('timeframe') or {}).get('text'), item.get('department'),
                ) if value
            )
            rows.append(f'<li><strong>{_e(_label(title))}</strong><span>{meta}{" " if meta else ""}{_e(detail)}'
                        f'{self.cluster_ref(item.get("clusterId")) if item.get("clusterId") else ""}</span></li>')
        return f'<ul class="as-gaps">{"".join(rows)}</ul>' if rows else ''

    def legacy_priorities(self, findings):
        """Older callbacks: P0 clusters as cards, then the authored priority paragraphs."""
        parts = []
        for cluster in self.core_clusters:
            if cluster.get('priority') != 'P0':
                continue
            clinical = [row for row in cluster.get('clinicalSignals') or ()
                        if not _is_advice(row)
                        and not any(source.get('sourceCode') == 'B' for source in row.get('sources') or ())]
            parts.append(self.action_card(
                f'{cluster.get("id")} {cluster.get("name")}', 'P0', basis=clinical,
                advice=_advice_quotes(cluster), text=cluster.get('action'), node=cluster,
            ))
        gaps = ''
        for finding in findings:
            text = finding.get('action') or finding.get('text')
            if finding.get('code') == 'gaps':
                gaps += self.paragraph(text, finding)
                continue
            priority = _FINDING_PRIORITIES.get(finding.get('code'))
            if priority == 'P0' and parts:
                continue
            parts.append(self.action_card(
                _text(finding.get('name')), priority, text=text, node=finding, routine=priority is None,
            ))
        html = f'<div class="as-actions">{"".join(parts)}</div>' if parts else ''
        if not any('class="as-act p0"' in part for part in parts):
            html = _note('本次未发现需优先就医排查的结果。') + html
        return html + (f'<h4 class="as-sub">检测盲区</h4>{gaps}' if gaps else '')

    def action_card(self, title, priority, *, timeframe=None, department=None, checks=None, basis=None,
                    advice=None, text=None, node=None, routine=False):
        rows = []
        if timeframe:
            rows.append(f'<dt>时限</dt><dd><span class="as-when">{_e(timeframe)}</span></dd>')
        if department:
            rows.append(f'<dt>科室</dt><dd>{_e(department)}</dd>')
        if checks:
            rows.append(f'<dt>需完成</dt><dd>{_e("；".join(_text(check) for check in checks))}</dd>')
        if basis:
            rows.append(f'<dt>依据</dt><dd>{self.tags(basis)}</dd>')
        if advice:
            rows.append(f'<dt>检查建议</dt><dd>{"<br>".join(_e(quote) for quote in advice)}</dd>')
        details = f'<dl>{"".join(rows)}</dl>' if rows else ''
        reading = self.paragraph(text, node)
        css = 'p0' if priority == 'P0' else 'routine' if routine else ''
        chip = _priority_chip(priority) if priority else '<span class="as-pri pri-p3">常规随访</span>'
        return (
            f'<article class="as-act {css}"><header>{chip}<h4>{_e(_label(title))}</h4></header>'
            f'{details}{reading}</article>'
        )

    def schedule(self, followups):
        head = ''.join(f'<th class="c">{_e(label)}</th>' for label, _ in _SCHEDULE)
        rows = []
        for item in followups:
            interval = item.get('interval') or item.get('timeframe') or {}
            marks = _schedule_marks(interval.get('days'))
            symbols = {'first': '<span class="mk"></span>', 're': '<span class="mk re"></span>'}
            cells = ''.join(
                f'<td class="c">{symbols.get(marks.get(index), "")}</td>' for index in range(len(_SCHEDULE))
            )
            rows.append(
                f'<tr><td><b>{_e(item.get("name") or item.get("title"))}</b><br><small class="as-muted">'
                f'{_e(" · ".join(_text(value) for value in (interval.get("text"), item.get("department")) if value))}'
                f'</small></td>{cells}</tr>'
            )
        legend = ('<div class="legend"><span><i class="mk"></i>首次复查</span>'
                  '<span><i class="mk re"></i>按间隔再次复查</span></div>')
        return (
            f'<table class="sched"><thead><tr><th>事项</th>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table>'
            + legend
        )

    def hub_lines(self):
        lines = []
        body_rows = [row for group in self.clinical_data.get('bodyGroups') or () for row in group.get('rows') or ()]
        for pattern in (r'BMI|体重指数', r'内脏脂肪'):
            row = next((row for row in body_rows if re.search(pattern, _text(row.get('name')))), None)
            if row and charts.parse_number(row.get('value')) is not None:
                text = f'{charts.display_name(row["name"])} {_text(row["value"])} {_unit(row.get("unit"))}'
                lines.append(text.strip())
        for group in self.microbiome_data.get('structureGroups') or ():
            if group.get('code') != 'overallBalance':
                continue
            score = next((row for row in group.get('rows') or () if row.get('code') == 'microbiomeHealthScore'), None)
            grade = next((row for row in group.get('rows') or () if row.get('code') == 'balanceStatus'), None)
            if score:
                grade_text = f'（{_text(grade.get("value"))}）' if grade else ''
                lines.append(f'菌群总分 {_text(score.get("value"))}{grade_text}')
        return lines[:3]

    def node_anchors(self, cluster):
        """Up to two abnormal measured signals of a cluster, as short text for its graph node."""
        texts = []
        for row, microbiome in [*((row, False) for row in cluster.get('clinicalSignals') or ()),
                                *((row, True) for row in cluster.get('microbiomeSignals') or ())]:
            if _is_advice(row) or any(source.get('sourceCode') == 'B' for source in row.get('sources') or ()):
                continue
            fields = charts.microbiome_chart_fields(row) if microbiome else charts.clinical_chart_fields(row)
            if fields.get('level') not in {charts.FOCUS, charts.BORDERLINE}:
                continue
            name = charts.display_name(clinical_observation_display_name(row) or row.get('name'))
            value = _text(row.get('value')).strip()
            text = f'{name} {value}' if value and len(value) <= 10 and value not in name else name
            if text not in texts:
                texts.append(text)
            if len(texts) == 2:
                break
        return texts

    def problem_graph(self):
        ids = self.overview_data.get('graphClusterIds')
        clusters = [item for item in self.core_clusters if ids is None or item.get('id') in ids] or self.core_clusters
        if not clusters:
            return _note('本次各系统未形成需集中干预的失衡方向。')
        nodes = [
            (_text(item.get('id')), _label(item.get('name')), item.get('priority'), self.node_anchors(item))
            for item in clusters
        ]
        hub = _text(self.overview_data.get('graphHub')) or '核心失衡态势'
        summary = self.paragraph(
            self.overview_data.get('graphSummary'), {'evidenceRefs': self.overview_data.get('graphSummaryEvidenceRefs')}
        )
        figure = (
            f'<figure class="as-graph">{_graph_svg(hub, self.hub_lines(), nodes)}'
            '<figcaption>左侧为本次关键指标，右侧为失衡集群及优先级；各集群的证据与解读见第五章。</figcaption></figure>'
        )
        return summary + figure

    def overview(self):
        return (
            _subsection('1.1 基础体格与人体成分深度校准', self.body_measurements())
            + _subsection('1.2 多系统健康状态分级总览', self.systems())
            + _subsection('1.3 临床优先排查项与未检风险提示', self.priority_cards())
            + _subsection('1.4 核心系统失衡态势速览', self.problem_graph())
        )

    # ----- chapter two -----

    def symptoms(self):
        clinical = self.clinical_data
        symptoms = [item for item in clinical.get('symptoms') or () if item.get('name') or item.get('value')]
        lead = self.paragraph(clinical.get('symptomsLead'))
        if not symptoms:
            return (
                _subsection('2.1 主诉症状谱与临床检验信号印证', _note('本次未记录主诉症状。'))
                + _subsection(
                    '2.2 主要异常与日常感受的关联',
                    self.paragraph(clinical.get('symptomSummary'), {
                        'evidenceRefs': clinical.get('symptomSummaryEvidenceRefs')
                    }) or _note('本次未记录主诉症状，本节不作对照说明。'),
                )
            )
        rows, readings = [], []
        for index, item in enumerate(symptoms, start=1):
            code = _text(item.get('code')) or f'S{index}'
            name = _text(item.get('name'))
            context = _qa_html(item.get('history'))
            if item.get('value') and _text(item.get('value')) not in name:
                context = f'<span class="qa">表现：{_e(item["value"])}</span>' + context
            signals = item.get('clinicalSignals') or ()
            personal = [row for row in signals if any(src.get('sourceCode') == 'B' for src in row.get('sources') or ())]
            context += ''.join(
                f'<span class="qa">{_e(_prompt(row.get("name")))}：{_e(row.get("value"))}</span>'
                for row in personal if row.get('value') and _text(row.get('value')) not in context
            )
            observed = [row for row in signals if row not in personal]
            rows.append(
                f'<tr><td><span class="as-cid">{_e(code)}</span> <b>{_e(name)}</b></td><td>{context or "—"}</td>'
                f'<td>{self.tags(observed) or "<span class=as-muted>—</span>"}</td>'
                f'<td>{self.tags(item.get("microbiomeSignals"), microbiome=True) or "<span class=as-muted>—</span>"}'
                '</td></tr>'
            )
            explanations = [self.paragraph(item.get('interpretation'), item)]
            for signal in [*(item.get('clinicalSignals') or ()), *(item.get('microbiomeSignals') or ())]:
                if signal.get('interpretation'):
                    explanations.append(self.paragraph(signal['interpretation'], signal))
            body = ''.join(dict.fromkeys(part for part in explanations if part))
            if body:
                readings.append(f'<li><h4>{_e(code)} {_e(name)}的相关线索</h4>{body}</li>')
        matrix = (
            '<table class="as-matrix"><colgroup><col style="width:17%"><col style="width:27%"><col style="width:28%">'
            '<col style="width:28%"></colgroup><thead><tr><th>主诉</th><th>表现、持续与加重情境</th>'
            '<th>临床检验与影像信号</th><th>菌群与代谢证据</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>'
        )
        details = f'<ol class="as-readings">{"".join(readings)}</ol>' if readings else ''
        summary = self.paragraph(
            clinical.get('symptomSummary'), {'evidenceRefs': clinical.get('symptomSummaryEvidenceRefs')}
        )
        if summary:
            details += f'<div class="as-note"><h5>综合印证</h5>{summary}</div>'
        return (
            _subsection('2.1 主诉症状谱与临床检验信号印证', lead + matrix)
            + _subsection('2.2 躯体感知背后的微生态与代谢线索', details or _note('本节暂无可对照的线索说明。'))
        )

    # ----- chapter three -----

    def clinical(self):
        clinical = self.clinical_data
        groups = {group.get('code'): group for group in clinical.get('clinicalGroups') or ()}
        sections, missing = [], []
        for code, title in _CLINICAL_SECTIONS:
            group = groups.get(code) or {}
            panels = group.get('templateRows') or ()
            content = ''.join(
                self.panel(panel.get('name'), panel.get('rows'), panel, summary=panel.get('summary'))
                for panel in panels
            )
            if not content and group.get('rows'):
                content = self.panel(group.get('name') or title[4:], group.get('rows'))
            reading = self.paragraph(group.get('summary'), group)
            if reading:
                content += f'<div class="as-note">{reading}</div>'
            if content:
                sections.append(_subsection(title, content, anchor=f'as-system-{code}'))
            else:
                missing.append(_SECTION_TOPICS[code])
        intro = self.paragraph(clinical.get('summary'), clinical)
        if missing:
            intro += f'<p class="as-muted">本次资料未包含{_e("、".join(missing))}类检查，相应小节从略。</p>'
        return intro + ''.join(sections) + self.specialty() + self.trends()

    def specialty(self):
        clinical = self.clinical_data
        groups = {}
        for row in clinical.get('specialtyFindings') or ():
            name = _text(row.get('name'))
            parent, separator, _ = name.partition(' · ')
            groups.setdefault(parent if separator else '专项检测', []).append(row)
        content = ''
        for title, rows in groups.items():
            readings = ''.join(dict.fromkeys(self.paragraph(row.get('interpretation'), row) for row in rows))
            content += self.panel(title, rows, extra=f'<div class="as-interp">{readings}</div>' if readings else '')
        other = [row for group in clinical.get('clinicalGroups') or ()
                 if group.get('code') == _OTHER_SYSTEM[0] for row in group.get('rows') or ()]
        if other:
            content += self.panel('其他检查', other)
        content += self.paragraph(clinical.get('specialtySummary'), {
            'evidenceRefs': clinical.get('specialtySummaryEvidenceRefs')
        })
        return _subsection('3.5 专项检测与筛查', content or _note('本次资料未包含设备专项检测与筛查项目。'))

    def trends(self):
        clinical = self.clinical_data
        cards = []
        for item in clinical.get('trends') or ():
            points = item.get('points') or ()
            chart = _trend_svg(points, _text(item.get('name')))
            if not chart:
                continue
            reading = self.paragraph(item.get('interpretation'), item)
            cards.append(
                f'<div class="as-trend"><h4>{_e(item.get("name"))} <small class="as-muted">{_e(item.get("direction"))}'
                f'</small></h4>{chart}{reading}</div>'
            )
        no_trend = '本次资料未包含可比较的历次结果。'
        trend = f'<div class="as-trends">{"".join(cards)}</div>' if cards else _note(no_trend)
        if (clinical.get('trendSummary') or '').strip() != no_trend:
            trend += self.paragraph(
                clinical.get('trendSummary'), {'evidenceRefs': clinical.get('trendSummaryEvidenceRefs')}
            )
        baselines = self.paragraph(clinical.get('baselineSummary'), {
            'evidenceRefs': clinical.get('baselineSummaryEvidenceRefs')
        })
        baselines += ''.join(
            self.normal_line(group.get('rows') or (), _label(group.get('name')))
            for group in clinical.get('normalBaselines') or ()
            if group.get('rows')
        )
        if baselines:
            trend += f'<h4 class="as-sub">已确认的稳健安全基线</h4>{baselines}'
        return _subsection('3.6 历年指标演变趋势与已确认稳健安全基线', trend)

    # ----- chapter four -----

    def microbiome(self):
        gut = self.microbiome_data
        has_data = any(gut.get(key) for key in ('structureGroups', 'structure', 'organismGroups', 'functionalGroups'))
        lead = self.paragraph(gut.get('overviewSummary'), {'evidenceRefs': gut.get('overviewSummaryEvidenceRefs')})
        if not has_data:
            digestive = _subsection('4.1 消化道器质基础与消化液微环境', self.digestive())
            return lead + _note('本次资料未包含肠道菌群检测。') + digestive
        sample = gut.get('sampleContext') or {}
        facts = [
            f'{label} {_text(sample[key])}'
            for key, label in (('sampleType', '样本'), ('sampleDate', '采样日'), ('receivedDate', '收样日'),
                               ('reportDate', '报告日'))
            if sample.get(key)
        ]
        notes = [f'<p>{_e("；".join(facts))}</p>'] if facts else []
        notes.append(self.paragraph(gut.get('sampleSummary'), {'evidenceRefs': gut.get('sampleSummaryEvidenceRefs')}))
        if not re.search(r'抗生素|泻药|益生菌', _text(gut.get('sampleSummary'))):
            notes.append('<p>采样前抗生素、泻药与益生菌使用情况未记录。</p>')
        fermentation = self.paragraph(gut.get('fermentationSummary'), {
            'evidenceRefs': gut.get('fermentationSummaryEvidenceRefs')
        })
        if fermentation:
            notes.append('<h5>发酵耐受</h5>' + fermentation)
        risk = self.paragraph(gut.get('riskSummary'), {'evidenceRefs': gut.get('riskSummaryEvidenceRefs')})
        if '临床检查结果为准' not in _text(gut.get('riskSummary')):
            risk += '<p>菌群风险模型的分值反映样本在模型中的位置，健康管理以临床检查结果为准。</p>'
        return (
            lead
            + f'<div class="as-note">{"".join(notes)}</div>'
            + _subsection('4.1 消化道器质基础与消化液微环境', self.digestive())
            + _subsection(self.structure_heading(), self.structure())
            + _subsection('4.3 核心功能菌群结构、条件致病谱与毒力因子', self.organisms())
            + _subsection('4.4 菌群营养合成与外源毒素代谢潜能', self.functions())
            + _subsection('本章风险小结', risk)
        )

    def structure_heading(self):
        codes = {group.get('code') for group in self.microbiome_data.get('structureGroups') or ()}
        if codes & {'developmentalMaturity', 'childGrowth'}:
            return '4.2 微生态宏观稳态指数与儿童相关菌群'
        if 'gutAge' in codes:
            return '4.2 微生态宏观稳态指数与肠道生物学年龄'
        return '4.2 微生态宏观稳态指数'

    def digestive(self):
        blocks = []
        for group in self.clinical_data.get('digestiveContext') or self.microbiome_data.get('digestiveContext') or ():
            context = '；'.join(
                f'{_prompt(row.get("name"))}：{_text(row.get("value"))}' for row in group.get('symptomContext') or ()
                if row.get('value')
            )
            extra = f'<p class="as-small as-muted">自述：{_e(context)}</p>' if context else ''
            blocks.append(self.panel(group.get('name'), _digestive_measurements(group), group,
                                     summary=group.get('summary'), extra=extra))
        content = ''.join(block for block in blocks if block)
        return content or _note('本次资料未包含口腔、胃肠与肝胆胰的相关检查。')

    def index_row(self, row, group):
        """One macro index: name | value + level | grade bands, reference band or plain axis | reference text."""
        fields = charts.microbiome_chart_fields(row)
        level = fields.get('level')
        number = fields.get('valueNumber')
        if number is None:
            number = charts.parse_number(row.get('value'))
        name = _label(charts.display_name(row.get('name')))
        label = f'{name} {_text(row.get("value"))}'
        chip = _level_chip(level) if level in {charts.FOCUS, charts.BORDERLINE} else ''
        status = charts.status_text(row)
        notes = []
        bands = fields.get('bands') or []
        if row.get('code') == 'gutAge' and number is not None:
            actual = row.get('actualAge', group.get('actualAge'))
            difference = row.get('ageDifference', group.get('gutAgeDifference'))
            if isinstance(actual, (int, float)):
                lo, hi = min(number, actual) - 10, max(number, actual) + 10
                ticks = [(actual, f'实足 {_fmt(actual)} 岁')]
                chart = _axis_svg(number, lo, hi, ticks, level, label, marker=actual)
                if isinstance(difference, (int, float)):
                    notes.append(f'与实足年龄相差 {"+" if difference > 0 else ""}{_fmt(difference)} 岁')
            else:
                chart = '<span class="gap">无对照年龄</span>'
        elif bands and number is not None:
            chart = _bands_svg(number, bands, level, label)
            notes.append('分档 ' + '；'.join(f'{_e(band["label"])} {_e(_band_text(band))}' for band in bands))
        elif number is not None and fields.get('referenceKind') in {'range', 'upper', 'lower'}:
            chart = _range_svg(number, fields['referenceLow'], fields['referenceHigh'], fields['referenceKind'], level,
                               label)
            notes.append(f'参考 {_e(row.get("referenceRange"))}')
        elif number is not None and 0 <= number <= 100 and re.search(r'评分|得分|总分', _text(row.get('name'))):
            chart = _axis_svg(number, 0, 100, [(0, '0'), (50, '50'), (100, '100')], level, label)
        else:
            chart = ''
        if status and not charts.is_normal_label(status) and status != _text(row.get('value')):
            notes.insert(0, f'判定 {_e(_label(status))}')
        value = _text(row.get('value')).strip()
        shown = (f'<b>{_e(value)}</b> {_e(_unit(row.get("unit")))}{_arrow(fields.get("direction"))}'
                 if number is not None else f'<b>{_e(value)}</b>')
        if not chart:
            return (f'<div class="as-mrow"><div class="nm">{_e(name)}</div><div class="val">{shown}{chip}</div>'
                    f'<div class="txt">{"<br>".join(notes)}</div></div>')
        return (f'<div class="as-mrow"><div class="nm">{_e(name)}</div><div class="val">{shown}{chip}</div>'
                f'<div class="ch">{chart}</div><div class="ref">{"<br>".join(notes)}</div></div>')

    def structure(self):
        gut = self.microbiome_data
        organism_codes = {
            row.get('code') for group in gut.get('organismGroups') or () for row in group.get('rows') or ()
        }
        blocks = []
        for group in gut.get('structureGroups') or ():
            rows = [row for row in group.get('rows') or ()
                    if not (charts.microbiome_kind(row) == 'abundance' and row.get('code') in organism_codes)]
            abundance = [row for row in rows if charts.microbiome_kind(row) == 'abundance']
            others = [row for row in rows if row not in abundance]
            body = ''.join(self.index_row(row, group) for row in others)
            body += ''.join(self.abundance_row(row) for row in abundance)
            if not body:
                continue
            level = charts.worst_level(charts.microbiome_chart_fields(row)['level'] for row in group.get('rows') or ())
            reading = self.paragraph(group.get('summary'), group)
            blocks.append(
                f'<div class="as-panel"><div class="as-panel-head"><h4>{_e(_label(group.get("name")))}</h4>'
                f'{_level_chip(level)}</div><div class="as-mrows">{body}</div>'
                f'{f"<div class=as-interp>{reading}</div>" if reading else ""}</div>'
            )
        summary = self.paragraph(gut.get('structureSummary'), {'evidenceRefs': gut.get('structureSummaryEvidenceRefs')})
        return ''.join(blocks) + summary or _note('本次菌群检测未包含宏观稳态指数。')

    def abundance_row(self, row):
        fields = charts.microbiome_chart_fields(row)
        level = fields.get('level')
        name = _label(row.get('name'))
        abundance = fields.get('abundance')
        value = _text(row.get('value')).strip()
        if abundance is not None and abundance <= 0:
            shown = '<b>0</b> %<small class="as-muted">（未检出）</small>'
        else:
            shown = f'<b>{_e(value)}</b> {_e(row.get("unit"))}{_arrow(fields.get("direction"))}'
        chip = ''  # a column of identical labels is noise; the dot and arrow carry the deviation
        reference = _text(row.get('referenceRange'))
        chart = _abundance_svg(abundance, fields.get('referenceLow'), fields.get('referenceHigh'), level,
                               f'{name} {value}%，参考 {reference}')
        return (f'<div class="as-mrow"><div class="nm">{_e(name)}</div><div class="val">{shown}{chip}</div>'
                f'<div class="ch">{chart}</div><div class="ref">{"参考 " + _e(reference) if reference else ""}</div>'
                '</div>')

    def organisms(self):
        blocks = []
        for group in self.microbiome_data.get('organismGroups') or ():
            rows = list(group.get('rows') or ())
            panels = [row for row in rows if charts.microbiome_kind(row) == 'panel'
                      and not organism_assessment_display_group(row) and not row.get('detectionStatus')]
            assessments = [row for row in rows if organism_assessment_display_group(row)]
            genes = [row for row in rows if row.get('detectionStatus') and charts.microbiome_kind(row) == 'panel'
                     and row not in assessments]
            taxa = [row for row in rows if charts.microbiome_kind(row) == 'abundance']
            body = ''
            if taxa:
                body += _abundance_axis() + ''.join(self.abundance_row(row) for row in taxa)
                body = f'<div class="as-mrows">{body}</div>'
            if genes or assessments:
                body += self.tags([*genes, *assessments], microbiome=True)
            baseline = group.get('normalBaseline') or {}
            normal_rows = baseline.get('rows') or ()
            names = baseline.get('names') or [row.get('name') for row in normal_rows]
            count = baseline.get('count') if type(baseline.get('count')) is int else len(names)
            if count:
                examples = [f'{_label(row.get("name"))} {_value(row)}'.strip() for row in normal_rows[:6]]
                examples = examples or [_label(name) for name in names[:6]]
                body += (
                    f'<div class="as-normal">{_ICONS["normal"]}<span><b>{count} 项在参考范围内</b>　'
                    f'{_e("；".join(examples))}{"等" if count > len(examples) else ""}</span></div>'
                )
            undetected = [row.get('name') for row in baseline.get('undetectedRows') or () if row.get('name')]
            if undetected:
                body += f'<p class="as-small as-muted">未检出序列信号：{_e("、".join(undetected))}</p>'
            reading = self.paragraph(group.get('summary'), group)
            if not body and not reading:
                continue
            level = charts.worst_level(charts.microbiome_chart_fields(row)['level'] for row in rows)
            panel_chips = ''.join(
                f'<span class="as-tag lv-{_level_class(charts.microbiome_chart_fields(row)["level"])}"><i></i>'
                f'{_e(_label(charts.display_name(row.get("name"))))} <b>{_e(_value(row))}</b></span>'
                for row in panels
            )
            blocks.append(
                f'<div class="as-panel"><div class="as-panel-head"><h4>{_e(_label(group.get("name")))}</h4>'
                f'{_level_chip(level)}</div>{f"<div class=as-tags>{panel_chips}</div>" if panel_chips else ""}{body}'
                f'{f"<div class=as-interp>{reading}</div>" if reading else ""}</div>'
            )
        return ''.join(blocks) or _note('本次菌群检测未包含功能菌群与病原谱。')

    def functions(self):
        blocks = []
        for group in self.microbiome_data.get('functionalGroups') or ():
            baseline = group.get('normalBaseline') or {}
            rows = [*(group.get('rows') or ()), *(baseline.get('rows') or ())]
            seen, scored = set(), []
            for row in rows:
                identity = (_text(row.get('name')), _text(row.get('value')))
                if identity in seen:
                    continue
                seen.add(identity)
                fields = charts.microbiome_chart_fields(row)
                if fields.get('score') is not None:
                    scored.append((row, fields))
            if not scored:
                reading = self.paragraph(group.get('summary'), group)
                if reading:
                    blocks.append(f'<div class="as-panel"><div class="as-panel-head"><h4>{_e(group.get("name"))}</h4>'
                                  f'</div><div class="as-interp">{reading}</div></div>')
                continue
            deviating = sum(1 for _, fields in scored if fields.get('level') in {charts.FOCUS, charts.BORDERLINE})
            schemes = []
            for _, fields in scored:
                key = _band_key(fields.get('bands') or ())
                if key and key not in schemes:
                    schemes.append(key)
            ordered = sorted(scored, key=lambda item: item[1].get('level') not in {charts.FOCUS, charts.BORDERLINE})
            body = ''.join(self.score_row(row, fields) for row, fields in ordered)
            states = '；'.join(
                f'{_label(panel.get("sourceName"))}：{_label(panel.get("status"))}'
                for panel in group.get('panelStates') or () if panel.get('sourceName') and panel.get('status')
            )
            reading = self.paragraph(group.get('summary'), group)
            title = re.sub(r'^\d+\.\s*', '', _text(group.get('name')))
            keys = ''.join(f'<div class="as-bandkey">{key}</div>' for key in schemes)
            blocks.append(
                f'<div class="as-panel"><div class="as-panel-head"><h4>{_e(title)}</h4>'
                f'<small>{len(scored)} 项中 {deviating} 项偏离</small>'
                f'{_level_chip(charts.BORDERLINE if deviating else charts.NORMAL)}</div>'
                f'{f"<p class=as-refline>{_e(states)}</p>" if states else ""}{keys}'
                f'<div class="as-scores">{body}</div>'
                f'{f"<div class=as-interp>{reading}</div>" if reading else ""}</div>'
            )
        if not blocks:
            return _note('本次菌群检测未包含营养合成与代谢潜能评分。')
        return f'<p>{_e(_MICROBIOME_INDEX_NOTE)}</p>' + ''.join(blocks)

    def score_row(self, row, fields):
        level = fields.get('level')
        name = _label(row.get('name'))
        score = fields['score']
        bands = fields.get('bands') or [{'label': '正常', 'low': 0.0, 'high': 100.0}]
        chart = _score_svg(score, bands, level, f'{name} {_fmt(score)} 分')
        return (
            f'<div class="as-srow" data-lv="{_level_class(level)}"><span class="nm">{_e(name)}</span>{chart}'
            f'<span class="v"><b>{_e(_fmt(score))}</b>{_arrow(fields.get("direction"))}</span></div>'
        )

    # ----- chapter five -----

    def cluster_card(self, item):
        identifier = _text(item.get('id'))
        anchor = _cluster_anchor(item.get('id'))
        clinical = item.get('clinicalSignals') or ()
        personal = [
            row for row in clinical if any(source.get('sourceCode') == 'B' for source in row.get('sources') or ())
        ]
        observations = [row for row in clinical if row not in personal and not _is_advice(row)]
        factors = [factor for factor in self.data.get('lifestyleFactors') or ()
                   if identifier and identifier in (factor.get('clusterIds') or ())]
        life = ''.join(
            f'<span class="as-tag plain">{_e(_prompt(row.get("name")))}：{_e(_text(row.get("value")))}</span>'
            for row in personal if row.get('value')
        )
        life += ''.join(f'<span class="as-tag plain">{_e(factor.get("name"))}</span>' for factor in factors)
        columns = (
            ('临床与影像锚点', self.tags(observations)),
            ('微生态与代谢锚点', self.tags(item.get('microbiomeSignals'), microbiome=True)),
            ('相关生活因素', f'<div class="as-tags">{life}</div>' if life else ''),
        )
        evidence = ''.join(
            f'<div><h5>{_e(title)}</h5>{content or "<span class=as-muted>本次未见相关记录</span>"}</div>'
            for title, content in columns
        )
        notes = ''
        if reading := self.paragraph(item.get('interpretation'), item):
            notes += f'<h5>健康影响</h5>{reading}'
        action = item.get('action') or next(
            (row.get('action') for row in self.conclusion_data.get('priorities') or ()
             if row.get('id') == item.get('id') and row.get('action')), None,
        )
        if action:
            notes += f'<h5>处理方向</h5>{self.paragraph(action)}'
        advice = [quote for quote in _advice_quotes(item) if quote not in _text(action)]
        if advice:
            notes += '<h5>检查建议</h5>' + ''.join(f'<p>{_e(quote)}</p>' for quote in advice)
        root = f' id="{anchor}"' if anchor else ''
        return (
            f'<article class="as-cluster"{root}><header><span class="as-cid">{_e(identifier)}</span>'
            f'<h4>{_e(_label(item.get("name")))}</h4>{_priority_chip(item.get("priority"))}</header>'
            f'<div class="as-evidence">{evidence}</div>{f"<div class=notes>{notes}</div>" if notes else ""}</article>'
        )

    def clusters(self):
        summary = self.paragraph(
            self.data.get('clusterSummary'), {'evidenceRefs': self.data.get('clusterSummaryEvidenceRefs')}
        )
        cards = ''.join(self.cluster_card(item) for item in self.core_clusters)
        if not cards:
            cards = _note('未形成需集中干预的失衡集群。')
        return (
            _subsection('5.1 个体化系统失衡集群定性与健康影响解读', summary + cards)
            + _subsection('5.2 ATM 健康诱因全景复盘与健康认知校准', self.lifestyle())
        )

    def lifestyle(self):
        factors = list(self.data.get('lifestyleFactors') or ())
        background = [factor for factor in factors if factor.get('kind') in _BACKGROUND_KINDS]
        habits = [factor for factor in factors if factor not in background]
        history = self.paragraph(self.data.get('lifestyleSummary'), {
            'evidenceRefs': self.data.get('lifestyleSummaryEvidenceRefs')
        })
        history += ''.join(
            f'<p><strong>{_e(factor.get("name"))}</strong>　{_e(factor.get("value"))}</p>'
            + self.paragraph(factor.get('interpretation'), factor)
            for factor in background
        )
        content = f'<h4 class="as-sub">先天背景与触发事件</h4>{history}' if history else ''
        assigned = {code: [] for code, _, _ in _LIFESTYLE_DIMENSIONS}
        others = []
        for factor in habits:
            text = _text(factor.get('name'))
            code = next((code for code, _, pattern in _LIFESTYLE_DIMENSIONS if re.search(pattern, text)), None)
            if code is None:
                code = next((code for code, _, pattern in _LIFESTYLE_DIMENSIONS
                             if re.search(pattern, _text(factor.get('value')))), None)
            (assigned[code] if code else others).append(factor)
        ids = [_text(item.get('id')) for item in self.core_clusters if item.get('id')]
        head = ''.join(f'<th class="c">{_e(identifier)}</th>' for identifier in ids)
        rows = []
        dimensions = [(code, label, assigned[code]) for code, label, _ in _LIFESTYLE_DIMENSIONS]
        if others:
            dimensions.append(('other', '其他生活因素', others))
        for _, label, members in dimensions:
            behaviour = ''.join(_qa_html(factor.get('value')) for factor in members)
            behaviour = behaviour or '<span class="as-muted">未记录</span>'
            linked = {identifier for factor in members for identifier in factor.get('clusterIds') or ()}
            hit = '<span class="as-hit" role="img" aria-label="相关"></span>'
            cells = ''.join(f'<td class="c">{hit if identifier in linked else ""}</td>' for identifier in ids)
            names = '、'.join(_text(factor.get('name')) for factor in members)
            rows.append(
                f'<tr><td><b>{_e(label)}</b>{f"<br><small class=as-muted>{_e(names)}</small>" if names else ""}</td>'
                f'<td>{behaviour}</td>{cells}</tr>'
            )
        width = max(46 - 5 * len(ids), 30)
        cols = '<col style="width:20%">' + f'<col style="width:{width}%">' + ''.join(
            '<col style="width:5em">' for _ in ids)
        content += (
            '<h4 class="as-sub">日常维持诱因与集群对应</h4>'
            f'<table class="as-matrix as-lsm"><colgroup>{cols}</colgroup><thead><tr><th>诱因维度</th>'
            f'<th>行为特征</th>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table>'
        )
        if ids:
            content += '<p class="as-small as-muted">圆点表示该维度与对应集群相关；集群编号见 5.1。</p>'
        readings = ''.join(
            f'<li><strong>{_e(factor.get("name"))}</strong>{self.paragraph(factor.get("interpretation"), factor)}</li>'
            for factor in habits if factor.get('interpretation')
        )
        if readings:
            content += f'<ul class="as-readings">{readings}</ul>'
        recognition = ''.join(
            f'<li><strong>{_e(row.get("name"))}</strong>{self.paragraph(row.get("text"), row)}</li>'
            for row in self.data.get('lifestyleRecognition') or () if row.get('text')
        )
        if recognition:
            content += f'<div class="as-note"><h5>健康认知校准</h5><ol>{recognition}</ol></div>'
        return content

    # ----- chapter six -----

    def stage_items(self):
        """Roadmap entries per stage: 1.3 items in the first stage, clusters by ``layers[].clusterIds``."""
        stages = {code: [] for code, *_ in _STAGES}
        layers = {code: [] for code, *_ in _STAGES}
        by_id = {item.get('id'): item for item in self.core_clusters}
        placed = set()
        for section in self.overview_data.get('priorityFindings') or ():
            for item in section.get('items') or ():
                name = _text(item.get('name') or item.get('title'))
                if section.get('code') == 'gaps':
                    name = f'补查 {name}'
                when = (item.get('timeframe') or {}).get('text')
                stages['s1'].append((name, item.get('priority'), when, _text(item.get('department'))))
                if item.get('clusterId'):
                    placed.add(item['clusterId'])

        def add_cluster(stage, cluster):
            placed.add(cluster.get('id'))
            title = f'{_text(cluster.get("id"))} {_text(cluster.get("name"))}'
            stages[stage].append((title, cluster.get('priority'), None, ''))

        for layer in self.conclusion_data.get('layers') or ():
            stage = _LAYER_STAGES.get(layer.get('code'), 's3')
            layers[stage].append(layer)
            for identifier in layer.get('clusterIds') or ():
                if identifier in by_id and identifier not in placed:
                    add_cluster(stage, by_id[identifier])
        for cluster in self.core_clusters:
            if cluster.get('id') not in placed:
                add_cluster(_PRIORITY_STAGES.get(cluster.get('priority'), 's3'), cluster)
        return stages, layers

    def conclusion(self):
        conclusion = self.conclusion_data
        summary = self.paragraph(conclusion.get('summary'), conclusion, css='as-lead')
        stages, layers = self.stage_items()
        columns, details = [], []
        for code, level_name, timeframe, title, empty in _STAGES:
            entries = ''.join(
                f'<li><strong>{_e(_label(name))}</strong>'
                + (f'{_priority_chip(priority)} ' if priority else '')
                + (f'<span class="as-when">{_e(when)}</span> ' if when else '')
                + (f'<span>{_e(detail)}</span>' if detail else '')
                + '</li>'
                for name, priority, when, detail in stages[code]
            )
            texts = [layer for layer in layers[code] if _text(layer.get('text')).strip()]
            if entries:
                body = f'<ul>{entries}</ul>'
                details.extend(
                    f'<h4>{_e(layer.get("name") or f"{level_name}（{timeframe}）")}</h4>'
                    + self.paragraph(layer.get('text'), layer)
                    for layer in texts
                )
            else:
                # An empty stage keeps the projection's own sentence when it has one.
                body = ''.join(self.paragraph(layer.get('text'), layer) for layer in texts) or f'<p>{_e(empty)}</p>'
            columns.append(
                f'<section class="as-stage {code}"><header><div class="t"><b>{_e(timeframe)}</b>'
                f'<span>{_e(level_name)}</span></div><h4>{_e(title)}</h4></header>{body}</section>'
            )
        roadmap = f'<div class="as-roadmap" aria-label="三阶段干预路线">{"".join(columns)}</div>'
        if details:
            roadmap += f'<div class="as-layers">{"".join(details)}</div>'
        directions = []
        urgent = any(item.get('priority') == 'P0' for item in self.core_clusters) or any(
            item.get('priority') == 'P0'
            for section in self.overview_data.get('priorityFindings') or () for item in section.get('items') or ()
        )
        for row in conclusion.get('managementDirections') or ():
            children = ''.join(
                f'<li><strong>{_e(child.get("name"))}</strong>{self.paragraph(child.get("text"), child)}</li>'
                for child in row.get('items') or () if child.get('text')
            )
            text = self.paragraph(row.get('text'), row)
            title = _text(row.get('name'))
            if row.get('items') is not None and not title.startswith('MTPN'):
                title = f'MTPN 个性化功能靶向配方方向（{title}）' if title else 'MTPN 个性化功能靶向配方方向'
            elif row.get('code') == 'lifestyle' and not title.startswith('LSMI'):
                title = f'LSMI 去诱因生活方式干预方向（{title}）' if title else 'LSMI 去诱因生活方式干预方向'
            if children or text:
                listed = f'<ol>{children}</ol>' if children else ''
                directions.append(f'<div class="as-direction"><h4>{_e(title)}</h4>{text}{listed}</div>')
        management = ''.join(directions)
        if urgent and '第一层级' not in management:
            management = '<p class="as-note">先完成第一层级的专科排查，再开始配方与生活方式干预。</p>' + management
        if not directions:
            management += _note('本次资料不足以形成配方与生活方式干预方向。')
        return (
            _subsection('6.1 核心健康风险分层总结', summary + roadmap)
            + _subsection('6.2 全周期微生态与生活方式协同管理方向', management)
        )

    # ----- appendix -----

    def reference_appendix(self):
        if not self.references:
            return ''
        rows = []
        for number, row in sorted(self.references.items()):
            fields = [row.get('title'), row.get('year')]
            if row.get('doi'):
                fields.append('DOI：' + _text(row['doi']))
            metadata = '；'.join(_e(value) for value in fields if value is not None and value != '')
            link = _reference_link(row)
            original = (
                f' <a href="{link}" target="_blank" rel="noopener noreferrer">原文链接</a>' if link else ''
            )
            rows.append(f'<li id="ref-{number}" value="{number}">{metadata}{original}</li>')
        return (
            '<section class="as-references" id="as-references" aria-labelledby="as-references-title">'
            '<h2 id="as-references-title">附录：参考文献</h2>'
            '<p class="as-small as-muted">正文方括号中的编号与下列文献一一对应。</p>'
            f'<ol class="as-reference-list">{"".join(rows)}</ol></section>'
        )


def _band_text(band):
    low, high = band.get('low'), band.get('high')
    if low is None:
        return f'<{_fmt(high)}'
    if high is None:
        return f'>{_fmt(low)}'
    return f'{_fmt(low)}–{_fmt(high)}'


def assessment_cover(body, presentation):
    """Cover: identity, the two source dates and whether they belong to one health cycle."""
    subject = body.get('subject') or {}
    sample = body.get('sample') or {}
    timing = (presentation.get('overview') or {}).get('dataTiming') or {}
    context = (presentation.get('microbiome') or {}).get('sampleContext') or {}
    age = subject.get('age')
    clinical_date = _text(timing.get('clinicalDate'))
    institution = _text(timing.get('clinicalInstitution'))
    sampled = _text(timing.get('microbiomeSampleDate') or sample.get('sampleDate') or context.get('sampleDate'))
    received = _text(timing.get('microbiomeReceivedDate') or context.get('receivedDate'))
    reported = _text(timing.get('microbiomeReportDate') or sample.get('reportDate') or context.get('reportDate'))
    gut_date, gut_label = (sampled, '菌群采样日期') if sampled else (
        (received, '菌群收样日期') if received else (reported, '菌群报告日期'))
    cells = [
        ('受检者', subject.get('name')),
        ('性别 / 年龄', ' · '.join(_text(value) for value in (subject.get('gender'),
                                                              f'{age} 岁' if age not in (None, '') else '') if value)),
        ('档案编码', sample.get('sampleId')),
        ('检测技术', sample.get('method')),
        ('临床体检日期', '　'.join(part for part in (clinical_date, institution) if part)),
        (gut_label, gut_date + ('（采样日期未注明）' if gut_date and not sampled else '')),
    ]
    rows = ''.join(f'<dt>{_e(label)}</dt><dd>{_e(value)}</dd>' for label, value in cells if value)
    strip = ''
    days = _days_between(clinical_date, gut_date) if clinical_date and gut_date else None
    if days is not None:
        months = days / 30.44
        basis = '' if sampled else '（按收样日期计算）' if gut_date == received else '（按报告日期计算）'
        if months <= 6:
            verdict = '两份资料属同一健康周期，可交叉印证'
        else:
            verdict = '两份资料间隔超过 6 个月，结论以较新的资料为准'
        strip = (
            f'<div class="as-timing"><b>资料时效</b><span>临床体检与菌群检测间隔约 {_fmt(round(months, 1))} 个月'
            f'{_e(basis)}</span><span>{_e(verdict)}</span></div>'
        )
    title = presentation.get('title') or '个体化全息健康与肠道微生态深度评估报告'
    return f'<header class="as-cover"><h1>{_e(title)}</h1><dl class="as-id">{rows}</dl>{strip}</header>'


def assessment_chapters(data, presentation, *, status=None, partial_reasons=()):
    renderer = _AssessmentRenderer(data, presentation, status=status, partial_reasons=partial_reasons)
    chapters = []
    for anchor, title, method_name in ASSESSMENT_SECTIONS:
        content = getattr(renderer, method_name)()
        section = f'<section class="chapter" id="{anchor}"><h2>{_e(title)}</h2>{content}</section>'
        chapters.append((anchor, title, section))
    if chapters and (appendix := renderer.reference_appendix()):
        anchor, title, section = chapters[-1]
        chapters[-1] = (anchor, title, section + appendix)
    return chapters
