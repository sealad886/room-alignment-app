const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const colors = ["#74c9bd", "#dda96a", "#8faed2", "#b58ccc", "#91bd7c", "#d28282"];

const state = {
  view: "library", library: null, media: [], projects: [], group: [], groupName: null, sources: [], selectedSource: 0,
  selectedSegment: 0, playhead: 30, playing: false, timer: null, duration: 31,
  project: null, preflight: null, anchor: "wall-clock", issue: null,
};

function toast(message) {
  const node = $("#toast"); node.textContent = message; node.classList.add("show");
  clearTimeout(toast.timer); toast.timer = setTimeout(() => node.classList.remove("show"), 2800);
}

async function api(path, options = {}) {
  const response = await fetch(path, {headers: {"Content-Type": "application/json"}, ...options});
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.message || `Request failed: ${response.status}`);
  return payload;
}

function formatTime(seconds = 0) {
  const ms = Math.round(seconds * 1000); const h = Math.floor(ms / 3600000);
  const m = Math.floor(ms / 60000) % 60; const s = Math.floor(ms / 1000) % 60;
  return `${String(h).padStart(2,"0")}:${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}.${String(ms % 1000).padStart(3,"0")}`;
}

function safe(value) { return String(value ?? "—").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function mediaLabel(media) { return media.camera || media.relative_path.split("/").at(-2) || "Unlabelled source"; }
function groupKey(media) { return (media.captured_at || "Undated").slice(0, 10); }

function showView(view) {
  if (view !== "library" && !state.project) return toast("Choose an indexed event first");
  state.view = view;
  $$(".view").forEach(node => node.classList.toggle("active", node.id === `${view}-view`));
  $$(".workflow").forEach(node => node.classList.toggle("active", node.dataset.view === view));
  if (view === "review") refreshReview();
  $("#footer-status").textContent = view === "library" ? "Library ready" : `${state.project.name} · ${view[0].toUpperCase()+view.slice(1)} decisions autosaved locally`;
}

async function loadLibraries() {
  const [libraries, projects] = await Promise.all([api("/api/libraries"), api("/api/projects")]);
  state.projects = projects;
  renderRecentProjects();
  if (!libraries.length) return;
  state.library = libraries[0];
  state.media = await api(`/api/media?libraryId=${state.library.id}&limit=2000`);
  renderLibrary();
}

function renderRecentProjects() {
  const section = $("#recent-projects"); section.classList.toggle("hidden", !state.projects.length);
  $("#recent-project-list").innerHTML = state.projects.slice(0, 5).map((project,index)=>`<button data-project="${index}"><strong>${safe(project.name)}</strong><small>${safe(project.cutAnchoring || "wall-clock")} · ${project.videoSegments?.length || 0} video segments</small></button>`).join("");
  $$("[data-project]").forEach(button=>button.onclick=()=>openProject(state.projects[Number(button.dataset.project)]));
}

async function openProject(project) {
  if (!state.library || state.library.id !== project.libraryId) {
    const libraries = await api("/api/libraries"); state.library = libraries.find(item=>item.id===project.libraryId);
    if (!state.library) return toast("Project library is not currently indexed");
    state.media = await api(`/api/media?libraryId=${state.library.id}&limit=2000`);
  }
  const mediaById = new Map(state.media.map(item=>[item.id,item]));
  const alignment = project.alignment || {};
  state.sources = Object.entries(alignment).map(([mediaId,entry],index)=>({name:entry.label || mediaId,clips:[mediaById.get(mediaId)].filter(Boolean),offset:Number(entry.offsetMs||0),reference:Boolean(entry.reference),color:colors[index%colors.length],media:mediaById.get(mediaId)})).filter(source=>source.media);
  if (!state.sources.length) return toast("Project sources fall outside loaded index window; rescan or open its date first");
  state.project = project; state.anchor=project.cutAnchoring || "wall-clock"; state.duration=Math.max(...project.videoSegments.map(x=>x.end),1); state.selectedSource=0; state.selectedSegment=0;
  $$(".workflow").forEach(button=>button.disabled=false); $$('.radio input[name="anchor"]').forEach(radio=>radio.checked=radio.value===state.anchor);
  renderSources(); renderProgram(); showView("align"); toast("Project reopened from local state");
}

function renderLibrary() {
  $("#library-empty").classList.add("hidden"); $("#library-content").classList.remove("hidden");
  $("#library-count").textContent = `${state.media.length.toLocaleString()} indexed clips`;
  const groups = Object.groupBy(state.media, groupKey);
  $("#library-list").innerHTML = Object.entries(groups).slice(0, 12).map(([date, items], index) => `<button class="library-card${index===0?" active":""}" data-group="${safe(date)}"><strong>${safe(date)}</strong><small>${items.length} clips · ${new Set(items.map(mediaLabel)).size} inferred sources</small></button>`).join("");
  const first = Object.keys(groups)[0]; if (first) selectGroup(first);
  $$("[data-group]").forEach(button => button.onclick = () => { $$("[data-group]").forEach(x => x.classList.remove("active")); button.classList.add("active"); selectGroup(button.dataset.group); });
}

function selectGroup(key) {
  state.groupName = key;
  state.group = state.media.filter(item => groupKey(item) === key);
  $("#media-table").innerHTML = state.group.slice(0, 200).map(item => `<tr data-media="${item.id}"><td class="mono">${safe(item.captured_at?.replace("T"," ") || "Unknown")}</td><td>${safe(mediaLabel(item))}<br><small>${safe(item.relative_path)}</small></td><td class="mono">${item.duration == null ? "Unknown" : `${item.duration.toFixed(2)}s`}</td><td>${safe(item.video_codec || "?")} · ${safe(item.width || "?")}×${safe(item.height || "?")}${item.audio_codec ? `<br><small>${safe(item.audio_codec)} · ${safe(item.sample_rate || "?")} Hz</small>` : ""}</td><td><span class="evidence-count">${item.evidence.length} records</span>${item.warning ? `<br><small>△ ${safe(item.warning)}</small>` : ""}</td></tr>`).join("");
  $$("#media-table tr").forEach(row => row.ondblclick = () => createProject(key));
  $("#event-title").textContent = `${key} · source alignment`;
}

function createProject(groupName) {
  const bySource = Object.groupBy(state.group, mediaLabel);
  state.sources = Object.entries(bySource).slice(0, 6).map(([name, clips], index) => ({name, clips, offset: 0, reference: index === 0, color: colors[index], media: clips[0]}));
  if (!state.sources.length) return toast("Selected group has no indexed video");
  state.duration = Math.max(6, Math.min(120, ...state.sources.map(source => source.media.duration || 30)));
  const slice = state.duration / Math.min(4, state.sources.length || 1);
  const videoSegments = Array.from({length: Math.min(4, state.sources.length)}, (_, index) => ({
    id: `V-${String(index + 1).padStart(3,"0")}`, start: index * slice, end: index === Math.min(4,state.sources.length)-1 ? state.duration : (index+1)*slice,
    mediaId: state.sources[index].media.id, sourceIn: 0, syncOffsetMs: 0, transforms: ["video decode", "H.264 encode"],
  }));
  const audioSegments = videoSegments.map((segment, index) => ({...segment, id:`A-${String(index+1).padStart(3,"0")}`, linked:true, offsetMs:0, transforms:["audio decode", "AAC encode"]}));
  state.project = {id: crypto.randomUUID(), name:`${groupName} alignment`, libraryId:state.library.id, wallClockOrigin: state.group.find(x=>x.captured_at)?.captured_at || null, cutAnchoring:"wall-clock", alignment:{}, videoSegments, audioSegments};
  $$(".workflow").forEach(button => button.disabled = false);
  renderSources(); renderProgram(); saveProject(); showView("align");
}

function renderSources() {
  const rows = state.sources.map((source,index) => `<button class="source-row${index===state.selectedSource?" active":""}" data-source="${index}" style="--source-color:${source.color}"><i></i><strong>${safe(source.name)}${source.reference?" · REF":""}</strong><span>${source.offset>=0?"+":""}${source.offset} ms</span></button>`).join("");
  $("#source-list").innerHTML = rows; $("#cut-source-list").innerHTML = rows;
  const options = state.sources.map((source,index)=>`<option value="${source.media.id}">${safe(source.name)}</option>`).join("");
  $("#video-source").innerHTML = options; $("#audio-source").innerHTML = `<option value="">No audio / silence</option>${options}`;
  $$("[data-source]").forEach(button => button.onclick = () => { state.selectedSource = Number(button.dataset.source); renderSources(); renderSourceInspector(); renderProgram(); });
  $("#source-monitors").innerHTML = state.sources.map((source,index)=>`<button class="source-monitor${index===state.selectedSource?" active":""}" data-source="${index}" style="--source-color:${source.color};--source-dark:${source.color}22"><span>${safe(source.name)}</span><small>${source.offset>=0?"+":""}${source.offset} ms · ${safe(source.media.video_codec || "video")}</small></button>`).join("");
  $("#alignment-tracks").innerHTML = state.sources.map((source,index)=>trackMarkup(source,index)).join("");
  $("#source-evidence-tracks").innerHTML = `<div class="timeline-shell">${state.sources.map((source,index)=>trackMarkup(source,index)).join("")}</div>`;
  renderSourceInspector();
}

function trackMarkup(source,index) {
  const width = Math.max(8, Math.min(95, ((source.media.duration || 20) / state.duration) * 100));
  const left = Math.max(0, Math.min(92, 8 + source.offset / 100));
  return `<div class="track-row"><div class="track-label"><strong>${safe(source.name)}</strong><small>${source.offset>=0?"+":""}${source.offset} ms</small></div><div class="track-clips"><i class="clip" style="left:${left}%;width:${Math.min(width,100-left)}%;--source-color:${source.color}"></i></div></div>`;
}

function renderSourceInspector() {
  const source = state.sources[state.selectedSource]; if (!source) return;
  $("#selected-source-name").textContent = source.name; $("#sync-offset").value = source.offset;
  $("#confidence-label").textContent = source.media.captured_at ? `Timestamp evidence · ${source.media.evidence.length} records` : "Timestamp unknown · manual alignment";
  $("#provenance-panel").innerHTML = provenanceMarkup(source.media);
}

function provenanceMarkup(media) {
  return `<p class="eyebrow">Inspectable source</p><h2>${safe(mediaLabel(media))}</h2><div class="summary"><div class="summary-row"><span>Clip ID</span><strong class="mono">${safe(media.id)}</strong></div><div class="summary-row"><span>Relative path</span><strong>${safe(media.relative_path)}</strong></div><div class="summary-row"><span>Captured</span><strong>${safe(media.captured_at)}</strong></div><div class="summary-row"><span>Container</span><strong>${safe(media.video_codec)}${media.audio_codec?` / ${safe(media.audio_codec)}`:""}</strong></div><div class="summary-row"><span>Evidence</span><strong>${media.evidence.length}</strong></div></div><pre>${safe(JSON.stringify(media.evidence,null,2))}</pre>`;
}

function currentSegment() { return state.project?.videoSegments[state.selectedSegment]; }
function sourceForMedia(mediaId) { return state.sources.find(source => source.media.id === mediaId); }

function renderProgram() {
  if (!state.project) return;
  const duration = Math.max(...state.project.videoSegments.map(x=>x.end), 1); state.duration = duration;
  $("#video-lane").innerHTML = state.project.videoSegments.map((segment,index)=>segmentMarkup(segment,index,duration,false)).join("");
  $("#audio-lane").innerHTML = state.project.audioSegments.map((segment,index)=>segmentMarkup(segment,index,duration,true)).join("");
  $$(".segment[data-segment]").forEach(node => node.onclick = () => { state.selectedSegment = Number(node.dataset.segment); renderProgram(); });
  const segment = currentSegment() || state.project.videoSegments[0]; const source = segment ? sourceForMedia(segment.mediaId) : null;
  const selected = state.sources[state.selectedSource] || source;
  $("#program-source").textContent = source?.name || "Output gap"; $("#program-segment-id").textContent = segment?.id || "—";
  $("#program-monitor").style.setProperty("--source-color", source?.color || "#e48778"); $("#program-monitor").style.setProperty("--source-dark", `${source?.color || "#e48778"}33`);
  $("#edit-segment-name").textContent = segment?.id || "No segment"; $("#video-source").value = segment?.mediaId || "";
  const audio = state.project.audioSegments[state.selectedSegment]; $("#audio-source").value = audio?.mediaId || ""; $("#audio-offset").value = audio?.offsetMs || 0;
  $("#link-av").classList.toggle("active", audio?.linked !== false); $("#unlink-av").classList.toggle("active", audio?.linked === false);
  $("#audio-meta").innerHTML = `<strong>${audio?.mediaId ? safe(sourceForMedia(audio.mediaId)?.media.audio_codec || "Audio metadata unknown") : "Generated silence"}</strong><p>${audio?.linked===false?"Independent audio remains synchronized to output time.":"Audio follows video source and boundaries."}</p>`;
  $("#segment-provenance").innerHTML = segment && source ? `<p class="eyebrow">Segment provenance</p><h2>${segment.id}</h2>${provenanceMarkup(source.media)}<div class="summary-row"><span>Source in</span><strong>${formatTime(segment.sourceIn)}</strong></div><div class="summary-row"><span>Editorial</span><strong>${formatTime(segment.start)}–${formatTime(segment.end)}</strong></div>` : "";
  $("#review-source").textContent = source?.name || "Output gap"; $("#review-duration").textContent = formatTime(duration);
  $("#cut-camera").disabled = !selected || selected.media.id === segment?.mediaId;
  detectIssues();
}

function segmentMarkup(segment,index,duration,audio) {
  const source = sourceForMedia(segment.mediaId); const left=segment.start/duration*100; const width=(segment.end-segment.start)/duration*100;
  return `<button class="segment${index===state.selectedSegment?" active":""}" data-segment="${index}" style="left:${left}%;width:${width}%;--source-color:${source?.color||"#777"}"><strong>${safe(segment.id)} · ${safe(source?.name || "Silence")}</strong><small>${audio?(segment.linked?"⌁ linked":"unlinked"):"PROV"} · ${formatTime(segment.start)}</small></button>`;
}

function detectIssues() {
  const segments = [...state.project.videoSegments].sort((a,b)=>a.start-b.start); let cursor=0; let issue=null;
  for (const segment of segments) { if(segment.start > cursor+.001){issue={kind:"gap",start:cursor,end:segment.start};break} if(segment.start < cursor-.001){issue={kind:"overlap",start:segment.start,end:cursor};break} cursor=Math.max(cursor,segment.end); }
  state.issue=issue; const lane=$("#reconciliation-lane"); lane.classList.toggle("hidden",!issue);
  if(issue){lane.innerHTML=`<strong>△ ${issue.kind === "gap" ? "Output gap" : "Overlapping selections"}</strong> · ${formatTime(issue.start)}–${formatTime(issue.end)}<div class="issue-actions"><button id="repair-one">${issue.kind==="gap"?"Close gap":"Resolve overlap"}</button><button id="normalize-all">Normalize all boundaries</button></div>`; $("#program-status").className="tag invalid"; $("#program-status").textContent=`△ ${issue.kind}`; $("#repair-one").onclick=repairOne; $("#normalize-all").onclick=normalizeAll;} else {$("#program-status").className="tag valid";$("#program-status").textContent="✓ Valid coverage";}
}

function repairOne(){ if(!state.issue)return; const segments=[...state.project.videoSegments].sort((a,b)=>a.start-b.start); for(let i=1;i<segments.length;i++){if(Math.abs(segments[i].start-segments[i-1].end)>.001){segments[i].start=segments[i-1].end;break}} mirrorLinkedAudio(); renderProgram(); saveProject(); toast("Boundary repaired explicitly"); }
function normalizeAll(){const segments=[...state.project.videoSegments].sort((a,b)=>a.start-b.start);segments[0].start=0;for(let i=1;i<segments.length;i++)segments[i].start=segments[i-1].end;mirrorLinkedAudio();renderProgram();saveProject();toast("All boundaries normalized");}
function mirrorLinkedAudio(){state.project.audioSegments.forEach((audio,index)=>{if(audio.linked){audio.start=state.project.videoSegments[index].start;audio.end=state.project.videoSegments[index].end;audio.mediaId=state.project.videoSegments[index].mediaId;}})}

function cutToSelected() {
  const time=state.playhead/100*state.duration; const index=state.project.videoSegments.findIndex(s=>time>s.start+.05&&time<s.end-.05); if(index<0)return toast("Move playhead inside a Program segment");
  const segment=state.project.videoSegments[index]; const source=state.sources[state.selectedSource]; if(segment.mediaId===source.media.id)return;
  const newSegment={...segment,id:`V-${String(state.project.videoSegments.length+1).padStart(3,"0")}`,start:time,mediaId:source.media.id,sourceIn:Math.max(0,time-source.offset/1000),syncOffsetMs:source.offset}; segment.end=time;
  state.project.videoSegments.splice(index+1,0,newSegment); const audio=state.project.audioSegments[index]; if(audio?.linked){audio.end=time;state.project.audioSegments.splice(index+1,0,{...audio,id:`A-${String(state.project.audioSegments.length+1).padStart(3,"0")}`,start:time,mediaId:source.media.id});}
  state.selectedSegment=index+1;renderProgram();saveProject();toast(`Cut to ${source.name} at ${formatTime(time)}`);
}

async function saveProject() {
  if(!state.project)return; state.project.cutAnchoring=state.anchor; state.project.alignment=Object.fromEntries(state.sources.map(source=>[source.media.id,{label:source.name,offsetMs:source.offset,reference:source.reference}]));
  await api("/api/projects",{method:"POST",body:JSON.stringify(state.project)});
}

async function refreshReview() {
  await saveProject(); state.preflight=await api(`/api/projects/${state.project.id}/preflight`); const manifest=await api(`/api/projects/${state.project.id}/manifest`);
  $("#manifest-preview").textContent=JSON.stringify(manifest,null,2); $("#preflight-heading").textContent=state.preflight.valid?"Output plan valid":"Output plan needs attention";
  $("#review-summary").innerHTML=[["Output duration",formatTime(state.preflight.duration)],["Video cuts",state.preflight.videoCuts],["Independent audio edits",state.preflight.independentAudioEdits],["Boundary issues",state.preflight.issues.length],["Cut anchoring",state.anchor],["Render mode",state.preflight.renderMode]].map(([a,b])=>`<div class="summary-row"><span>${a}</span><strong>${safe(b)}</strong></div>`).join("");
  $("#preflight-status").innerHTML=state.preflight.valid?"<strong>✓ Coverage and provenance complete</strong><p>Review confirmation enables rendering.</p>":`<strong>△ Render blocked</strong><p>${state.preflight.issues.map(x=>safe(x.message)).join(" · ")}</p>`; updateRenderButton();
}

function updateRenderButton(){ $("#render-video").disabled=!state.preflight?.valid||!$("#reviewed-check").checked; }
async function renderVideo(){const button=$("#render-video");button.disabled=true;const result=await api(`/api/projects/${state.project.id}/render`,{method:"POST",body:JSON.stringify({outputPath:$("#output-path").value,lossless:$("#lossless-check").checked})});const panel=$("#render-progress");panel.classList.remove("hidden");const poll=setInterval(async()=>{const job=await api(`/api/render/${result.jobId}`);panel.textContent=`${job.status}: ${job.message||""}`;if(["complete","failed","canceled"].includes(job.status)){clearInterval(poll);updateRenderButton();toast(job.status==="complete"?"Render and provenance manifest complete":`Render ${job.status}`)}},800);}

function setupEvents() {
  $$("[data-view]").forEach(button=>button.onclick=()=>showView(button.dataset.view));
  $("#scan-form").onsubmit=async event=>{event.preventDefault();try{$("#scan-progress").classList.remove("hidden");const limit=$("#scan-limit").value;const {scanId}=await api("/api/scans",{method:"POST",body:JSON.stringify({path:$("#library-path").value,limit:limit?Number(limit):null})});const poll=setInterval(async()=>{const scan=await api(`/api/scans/${scanId}`);$("#scan-count").textContent=`${scan.count||0} clips`;if(scan.status==="complete"){clearInterval(poll);$("#scan-progress").classList.add("hidden");await loadLibraries();toast(`Indexed ${scan.summary.videos} videos with ${scan.summary.warnings} warnings`)}else if(scan.status==="failed"){clearInterval(poll);$("#scan-progress").classList.add("hidden");toast(scan.error)}},600)}catch(error){$("#scan-progress").classList.add("hidden");toast(error.message)}};
  $("#sync-offset").onchange=event=>{const source=state.sources[state.selectedSource];const before=source.offset;source.offset=Number(event.target.value);if(state.anchor==="source-clips"){const delta=(source.offset-before)/1000;state.project.videoSegments.filter(x=>x.mediaId===source.media.id).forEach(x=>{x.start+=delta;x.end+=delta});mirrorLinkedAudio()}renderSources();renderProgram();saveProject()};
  $$("[data-nudge]").forEach(button=>button.onclick=()=>{$("#sync-offset").value=Number($("#sync-offset").value)+Number(button.dataset.nudge);$("#sync-offset").dispatchEvent(new Event("change"))});
  $("#set-reference").onclick=()=>{state.sources.forEach((source,index)=>source.reference=index===state.selectedSource);renderSources();saveProject();toast(`${state.sources[state.selectedSource].name} is reference source`)};
  $$('input[name="anchor"]').forEach(radio=>radio.onchange=()=>{state.anchor=radio.value;state.project.cutAnchoring=radio.value;saveProject();toast(radio.value==="wall-clock"?"Cuts stay on output clock":"Cuts now follow their source clips")});
  $("#cut-camera").onclick=cutToSelected; $("#video-source").onchange=event=>{currentSegment().mediaId=event.target.value;const audio=state.project.audioSegments[state.selectedSegment];if(audio?.linked)audio.mediaId=event.target.value;renderProgram();saveProject()};
  $("#link-av").onclick=()=>{const video=currentSegment(),audio=state.project.audioSegments[state.selectedSegment];Object.assign(audio,{linked:true,mediaId:video.mediaId,start:video.start,end:video.end});renderProgram();saveProject()};
  $("#unlink-av").onclick=()=>{state.project.audioSegments[state.selectedSegment].linked=false;renderProgram();saveProject()};
  $("#audio-source").onchange=event=>{const audio=state.project.audioSegments[state.selectedSegment];audio.linked=false;audio.mediaId=event.target.value||null;renderProgram();saveProject()};
  $("#audio-offset").onchange=event=>{const audio=state.project.audioSegments[state.selectedSegment];audio.linked=false;audio.offsetMs=Number(event.target.value);renderProgram();saveProject()};
  $$("[data-boundary]").forEach(button=>button.onclick=()=>{const segment=currentSegment();const index=state.selectedSegment;if(!segment||index===0)return toast("Select a segment after first cut");const delta=Number(button.dataset.boundary);segment.start=Math.max(state.project.videoSegments[index-1].start+.05,segment.start+delta);if(state.anchor==="wall-clock")state.project.videoSegments[index-1].end=segment.start;mirrorLinkedAudio();renderProgram();saveProject()});
  $$("[data-action='play']").forEach(button=>button.onclick=togglePlay); $$("[data-action='rewind']").forEach(button=>button.onclick=()=>setPlayhead(state.playhead-1000/state.duration)); $$("[data-action='forward']").forEach(button=>button.onclick=()=>setPlayhead(state.playhead+1000/state.duration));
  $(".scrubber").oninput=event=>setPlayhead(Number(event.target.value)); $("#reviewed-check").onchange=updateRenderButton; $("#render-video").onclick=renderVideo;
  $("#download-manifest").onclick=()=>{const blob=new Blob([$("#manifest-preview").textContent],{type:"application/json"});const a=document.createElement("a");a.href=URL.createObjectURL(blob);a.download=`${state.project.name.replace(/\W+/g,"-").toLowerCase()}-manifest.json`;a.click();URL.revokeObjectURL(a.href)};
  $("#open-event").onclick=()=>createProject(state.groupName || "Selected event");
  document.addEventListener("keydown",event=>{if(["INPUT","SELECT","TEXTAREA"].includes(event.target.tagName))return;if(event.code==="Space"){event.preventDefault();togglePlay()}if(event.key.toLowerCase()==="c"&&state.view==="cut")cutToSelected()});
  $$("[data-inspector]").forEach(button=>button.onclick=()=>{$$("[data-inspector]").forEach(x=>x.classList.remove("active"));button.classList.add("active");$("#sync-panel").classList.toggle("hidden",button.dataset.inspector!=="sync");$("#provenance-panel").classList.toggle("hidden",button.dataset.inspector!=="provenance")});
  $$("[data-cut-panel]").forEach(button=>button.onclick=()=>{$$("[data-cut-panel]").forEach(x=>x.classList.remove("active"));button.classList.add("active");$("#edit-panel").classList.toggle("hidden",button.dataset.cutPanel!=="edit");$("#segment-provenance").classList.toggle("hidden",button.dataset.cutPanel!=="prov")});
}

function setPlayhead(value){state.playhead=Math.max(0,Math.min(100,value));$$(".playhead").forEach(node=>node.style.left=`${state.playhead}%`);$(".scrubber").value=state.playhead;const time=state.playhead/100*state.duration;$("#align-time").textContent=formatTime(time);$("#program-clock").textContent=`OUTPUT ${formatTime(time)}`;const index=state.project?.videoSegments.findIndex(s=>time>=s.start&&time<s.end);if(index>=0&&index!==state.selectedSegment){state.selectedSegment=index;renderProgram()}}
function togglePlay(){state.playing=!state.playing;$$('[data-action="play"]').forEach(button=>button.textContent=state.playing?"Ⅱ":"▶");clearInterval(state.timer);if(state.playing)state.timer=setInterval(()=>{setPlayhead(state.playhead+100/state.duration);if(state.playhead>=100)togglePlay()},100)}

setupEvents(); loadLibraries().catch(error=>toast(error.message));
