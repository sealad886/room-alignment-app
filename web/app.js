const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const colors = ["#74c9bd", "#dda96a", "#8faed2", "#b58ccc", "#91bd7c", "#d28282"];
const {RoomAlignmentAPIClient, APIError} = window.RoomAlignmentAPI;
const client = new RoomAlignmentAPIClient();

const state = {
  view: "library",
  library: null,
  media: [],
  mediaById: new Map(),
  mediaCursor: null,
  mediaGeneration: null,
  projects: [],
  groups: [],
  group: [],
  groupName: null,
  project: null,
  compiled: null,
  sources: [],
  selectedSource: 0,
  selectedSegment: 0,
  playhead: 30,
  playing: false,
  timer: null,
  durationUs: 1,
  renderPlan: null,
  artifact: null,
  scanJob: null,
  renderJob: null,
  eventCursor: 0,
};

function safe(value) {
  return String(value ?? "—").replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[character]));
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.remove("show"), 3600);
}

function formatUs(valueUs = 0) {
  const milliseconds = Math.round(Number(valueUs) / 1000);
  const hours = Math.floor(milliseconds / 3_600_000);
  const minutes = Math.floor(milliseconds / 60_000) % 60;
  const seconds = Math.floor(milliseconds / 1000) % 60;
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${String(Math.abs(milliseconds) % 1000).padStart(3, "0")}`;
}

function mediaLabel(media) {
  return media?.camera || media?.relative_path?.split("/").at(-2) || "Unlabelled source";
}

function groupKey(media) {
  return (media.captured_at || "Undated").slice(0, 10);
}

function groupBy(items, keyFunction) {
  const groups = new Map();
  for (const item of items) {
    const key = keyFunction(item);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  }
  return [...groups.entries()];
}

function currentOutputUs() {
  return Math.round((state.playhead / 100) * state.durationUs);
}

function sortedVideoBlocks() {
  return [...(state.project?.videoBlocks || [])].sort((left, right) => left.startUs - right.startUs);
}

function sortedAudioBlocks() {
  return [...(state.project?.audioBlocks || [])].sort((left, right) => left.startUs - right.startUs);
}

function currentVideoBlock() {
  return sortedVideoBlocks()[state.selectedSegment] || null;
}

function currentAudioBlock() {
  const outputUs = currentOutputUs();
  return sortedAudioBlocks().find(block => block.startUs <= outputUs && outputUs < block.endUs)
    || sortedAudioBlocks()[0]
    || null;
}

function sourceById(sourceId) {
  return state.sources.find(source => source.id === sourceId);
}

function sourceForBlock(block) {
  return block ? sourceById(block.logicalSourceId) : null;
}

function clipById(clipId) {
  return state.project?.clips.find(clip => clip.id === clipId);
}

function showView(view) {
  if (view !== "library" && !state.project) {
    toast("Choose indexed media first");
    return;
  }
  state.view = view;
  $$(".view").forEach(node => node.classList.toggle("active", node.id === `${view}-view`));
  $$(".workflow").forEach(node => node.classList.toggle("active", node.dataset.view === view));
  if (view === "review") prepareReview();
  $("#footer-status").textContent = view === "library"
    ? "Library ready"
    : `${state.project.name} · revision ${state.project.revision} · backend-authoritative ${view}`;
}

async function loadLibraries() {
  const [libraries, projects] = await Promise.all([client.libraries(), client.projects()]);
  state.projects = projects;
  renderRecentProjects();
  if (!libraries.length) return;
  state.library = libraries[0];
  await loadMediaPage(true);
}

async function loadMediaPage(reset = false) {
  if (!state.library) return;
  if (reset) {
    state.media = [];
    state.mediaById.clear();
    state.mediaCursor = null;
    state.mediaGeneration = null;
  }
  const query = new URLSearchParams({limit: "500"});
  if (state.mediaCursor) query.set("cursor", state.mediaCursor);
  if (state.mediaGeneration !== null) query.set("generation", String(state.mediaGeneration));
  const page = await client.media(state.library.id, query.toString());
  state.mediaGeneration = page.snapshotGeneration;
  state.mediaCursor = page.nextCursor;
  for (const media of page.items) {
    if (!state.mediaById.has(media.id)) state.media.push(media);
    state.mediaById.set(media.id, media);
  }
  $("#load-more-media").disabled = !state.mediaCursor;
  $("#load-more-media").textContent = state.mediaCursor ? "Load more" : "All loaded";
  renderLibrary(page.total);
}

function renderRecentProjects() {
  const section = $("#recent-projects");
  section.classList.toggle("hidden", !state.projects.length);
  $("#recent-project-list").innerHTML = state.projects.slice(0, 5).map((project, index) => `
    <button data-project="${index}">
      <strong>${safe(project.name)}</strong>
      <small>revision ${project.revision} · ${project.videoBlocks?.length || 0} video decisions</small>
    </button>`).join("");
  $$('[data-project]').forEach(button => {
    button.onclick = () => openProject(state.projects[Number(button.dataset.project)]);
  });
}

function renderLibrary(total = state.media.length) {
  $("#library-empty").classList.toggle("hidden", state.media.length > 0);
  $("#library-content").classList.toggle("hidden", state.media.length === 0);
  $("#library-count").textContent = `${state.media.length.toLocaleString()} of ${Number(total).toLocaleString()} clips loaded · generation ${state.mediaGeneration}`;
  state.groups = groupBy(state.media, groupKey);
  $("#library-list").innerHTML = state.groups.slice(0, 18).map(([date, items], index) => `
    <button class="library-card${index === 0 ? " active" : ""}" data-group-index="${index}">
      <strong>${safe(date)}</strong>
      <small>${items.length} clips · ${new Set(items.map(item => item.sourceCandidateId || item.id)).size} source candidates</small>
    </button>`).join("");
  if (!state.group.length && state.groups.length) selectGroup(0);
  else if (state.groupName) {
    const index = state.groups.findIndex(([name]) => name === state.groupName);
    if (index >= 0) selectGroup(index);
  }
  $$('[data-group-index]').forEach(button => {
    button.onclick = () => {
      $$('[data-group-index]').forEach(item => item.classList.remove("active"));
      button.classList.add("active");
      selectGroup(Number(button.dataset.groupIndex));
    };
  });
}

function selectGroup(index) {
  const selected = state.groups[index];
  if (!selected) return;
  [state.groupName, state.group] = selected;
  $("#media-table").innerHTML = state.group.slice(0, 500).map(item => `
    <tr data-media="${safe(item.id)}">
      <td class="mono">${safe(item.captured_at?.replace("T", " ") || "Unknown")}</td>
      <td>${safe(mediaLabel(item))}<br><small>${safe(item.relative_path)}</small></td>
      <td class="mono">${item.durationUs == null ? "Unknown" : formatUs(item.durationUs)}</td>
      <td>${safe(item.video_codec || "unsupported")}${item.width ? ` · ${item.width}×${item.height}` : ""}${item.audio_codec ? `<br><small>${safe(item.audio_codec)} · ${safe(item.sample_rate || "?")} Hz</small>` : "<br><small>No usable audio reported</small>"}</td>
      <td><span class="evidence-count">${item.evidence?.length || 0} observations</span>${item.warning ? `<br><small>△ ${safe(item.warning)}</small>` : ""}</td>
    </tr>`).join("");
  $$("#media-table tr").forEach(row => { row.ondblclick = createProjectFromGroup; });
  $("#event-title").textContent = `${state.groupName} · source alignment`;
}

async function ensureProjectMedia(project) {
  const missing = project.clips.map(clip => clip.assetId).filter(assetId => !state.mediaById.has(assetId));
  const records = await Promise.all(missing.map(assetId => client.mediaAsset(assetId)));
  for (const record of records) state.mediaById.set(record.id, record);
}

async function createProjectFromGroup() {
  if (!state.group.length) return toast("Selected group has no indexed video");
  try {
    const project = await client.createProject({
      name: `${state.groupName} alignment`,
      libraryId: state.library.id,
      assetIds: state.group.map(item => item.id),
    });
    state.projects = await client.projects();
    renderRecentProjects();
    await openProject(project);
    toast("Draft program created; unresolved coverage remains visible");
  } catch (error) {
    handleError(error);
  }
}

async function openProject(projectSummary) {
  try {
    const project = await client.project(projectSummary.id);
    const libraries = await client.libraries();
    state.library = libraries.find(item => item.id === project.libraryId) || state.library;
    await ensureProjectMedia(project);
    state.project = project;
    state.compiled = await client.program(project.id);
    state.durationUs = Math.max(1, state.compiled.durationUs);
    state.selectedSource = 0;
    state.selectedSegment = 0;
    state.renderPlan = null;
    state.artifact = null;
    deriveSources();
    $$(".workflow").forEach(button => { button.disabled = false; });
    $$('input[name="anchor"]').forEach(radio => {
      radio.checked = radio.value === (project.anchorMode === "SOURCE_TIME" ? "source-clips" : "wall-clock");
    });
    renderSources();
    renderProgram();
    showView("align");
    await setPlayhead(30);
  } catch (error) {
    handleError(error);
  }
}

function deriveSources() {
  state.sources = state.project.logicalSources.filter(source => !source.archived).map((source, index) => {
    const clips = state.project.clips.filter(clip => clip.logicalSourceId === source.id);
    const media = clips.map(clip => state.mediaById.get(clip.assetId)).filter(Boolean);
    const firstSync = clips[0]?.sync || {anchorOutputUs: 0, ratePpm: 0};
    return {
      ...source,
      clips,
      media,
      color: colors[index % colors.length],
      offsetUs: Number(firstSync.anchorOutputUs || 0),
      ratePpm: Number(firstSync.ratePpm || 0),
    };
  });
  if (state.selectedSource >= state.sources.length) state.selectedSource = 0;
}

function renderSources() {
  const rows = state.sources.map((source, index) => `
    <button class="source-row${index === state.selectedSource ? " active" : ""}" data-source="${index}" style="--source-color:${source.color}">
      <i></i><strong>${safe(source.label)}${source.reference ? " · REF" : ""}</strong>
      <span>${source.offsetUs >= 0 ? "+" : ""}${Math.round(source.offsetUs / 1000)} ms${source.ratePpm ? ` · ${source.ratePpm} ppm` : ""}</span>
    </button>`).join("");
  $("#source-list").innerHTML = rows;
  $("#cut-source-list").innerHTML = rows;
  $("#source-monitors").innerHTML = state.sources.map((source, index) => `
    <button class="source-monitor${index === state.selectedSource ? " active" : ""}" data-source="${index}" style="--source-color:${source.color};--source-dark:${source.color}22">
      <span>${safe(source.label)}</span><small>${source.clips.length} clips · ${source.offsetUs >= 0 ? "+" : ""}${Math.round(source.offsetUs / 1000)} ms</small>
    </button>`).join("");
  $("#alignment-tracks").innerHTML = state.sources.map(source => trackMarkup(source)).join("");
  $("#source-evidence-tracks").innerHTML = `<div class="timeline-shell">${state.sources.map(source => trackMarkup(source)).join("")}</div>`;
  const videoOptions = state.sources.map(source => `<option value="${safe(source.id)}">${safe(source.label)}</option>`).join("");
  $("#video-source").innerHTML = videoOptions;
  const fixedClipOptions = state.sources.flatMap(source => source.clips.map((clip, index) => `<option value="clip:${safe(clip.id)}">Clip ${index + 1} · ${safe(source.label)}</option>`)).join("");
  $("#audio-source").innerHTML = `<option value="follow">Follow Program Video</option><option value="silence">Intentional silence</option>${state.sources.map(source => `<option value="source:${safe(source.id)}">Fixed source · ${safe(source.label)}</option>`).join("")}${fixedClipOptions}`;
  $$('[data-source]').forEach(button => {
    button.onclick = () => {
      state.selectedSource = Number(button.dataset.source);
      renderSources();
      renderSourceInspector();
      renderProgram();
    };
  });
  renderSourceInspector();
}

function trackMarkup(source) {
  const clips = source.clips.map(clip => {
    const media = state.mediaById.get(clip.assetId);
    const sync = clip.sync || {};
    const startUs = Number(sync.anchorOutputUs || 0);
    const durationUs = Number(media?.durationUs || 0);
    const left = Math.max(0, Math.min(99, (startUs / state.durationUs) * 100));
    const width = Math.max(1, Math.min(100 - left, (durationUs / state.durationUs) * 100));
    return `<i class="clip" title="${safe(media?.relative_path)}" style="left:${left}%;width:${width}%;--source-color:${source.color}"></i>`;
  }).join("");
  return `<div class="track-row"><div class="track-label"><strong>${safe(source.label)}</strong><small>${source.clips.length} source clips</small></div><div class="track-clips">${clips}</div></div>`;
}

function renderSourceInspector() {
  const source = state.sources[state.selectedSource];
  if (!source) return;
  $("#selected-source-name").textContent = source.label;
  $("#sync-offset").value = Math.round(source.offsetUs / 1000);
  const media = source.media[0];
  $("#confidence-label").textContent = media?.captured_at
    ? `${source.clips.length} clips · timestamp evidence available`
    : `${source.clips.length} clips · manual timing required`;
  $("#provenance-panel").innerHTML = source.media.map(provenanceMarkup).join("");
}

function provenanceMarkup(media) {
  if (!media) return "<p>Source metadata unavailable.</p>";
  return `<p class="eyebrow">Inspectable source asset</p><h2>${safe(mediaLabel(media))}</h2>
    <div class="summary">
      <div class="summary-row"><span>Opaque asset ID</span><strong class="mono">${safe(media.id)}</strong></div>
      <div class="summary-row"><span>Library-relative path</span><strong>${safe(media.relative_path)}</strong></div>
      <div class="summary-row"><span>Captured evidence</span><strong>${safe(media.captured_at || "Unresolved")}</strong></div>
      <div class="summary-row"><span>Streams</span><strong>${media.streams?.length || 0}</strong></div>
      <div class="summary-row"><span>Evidence observations</span><strong>${media.evidence?.length || 0}</strong></div>
    </div><pre>${safe(JSON.stringify(media.evidence || [], null, 2))}</pre>`;
}

function segmentMarkup(block, index, durationUs, audio = false) {
  const left = (block.startUs / durationUs) * 100;
  const width = ((block.endUs - block.startUs) / durationUs) * 100;
  let source = sourceForBlock(block);
  let label = source?.label || "Unresolved";
  let color = source?.color || "#777";
  if (audio) {
    if (block.mode === "SILENCE") { label = "Intentional silence"; color = "#777"; }
    if (block.mode === "FOLLOW_VIDEO") { label = "Follow Program Video"; color = "#74c9bd"; }
    if (block.mode === "FIXED_CLIP") {
      source = sourceById(clipById(block.clipId)?.logicalSourceId);
      label = `Pinned clip · ${source?.label || "source"}`;
      color = source?.color || "#777";
    }
  }
  return `<button class="segment${!audio && index === state.selectedSegment ? " active" : ""}" data-${audio ? "audio" : "segment"}="${index}" style="left:${left}%;width:${width}%;--source-color:${color}">
    <strong>${safe(label)}</strong><small>${audio ? safe(block.mode) : `V${index + 1}`} · ${formatUs(block.startUs)}</small>
  </button>`;
}

function renderProgram() {
  if (!state.project || !state.compiled) return;
  const video = sortedVideoBlocks();
  const audio = sortedAudioBlocks();
  state.durationUs = Math.max(1, state.compiled.durationUs);
  const videoMarkup = video.map((block, index) => segmentMarkup(block, index, state.durationUs)).join("");
  const audioMarkup = audio.map((block, index) => segmentMarkup(block, index, state.durationUs, true)).join("");
  $("#video-lane").innerHTML = videoMarkup;
  $("#audio-lane").innerHTML = audioMarkup;
  $("#align-video-lane").innerHTML = videoMarkup.replaceAll("data-segment", "data-align-segment");
  $("#align-audio-lane").innerHTML = audioMarkup.replaceAll("data-audio", "data-align-audio");
  $$('[data-segment], [data-align-segment]').forEach(node => {
    node.onclick = () => {
      state.selectedSegment = Number(node.dataset.segment ?? node.dataset.alignSegment);
      const block = currentVideoBlock();
      if (block) setPlayhead((block.startUs / state.durationUs) * 100 + 0.01);
      renderProgram();
    };
  });
  const block = currentVideoBlock();
  const source = sourceForBlock(block);
  $("#program-source").textContent = source?.label || "Output gap";
  $("#program-segment-id").textContent = block?.id || "—";
  $("#program-monitor").style.setProperty("--source-color", source?.color || "#e48778");
  $("#program-monitor").style.setProperty("--source-dark", `${source?.color || "#e48778"}33`);
  $("#edit-segment-name").textContent = block ? `Video slice ${state.selectedSegment + 1}` : "No video decision";
  $("#video-source").value = block?.logicalSourceId || "";
  $("#review-source").textContent = source?.label || "Output gap";
  $("#review-duration").textContent = formatUs(state.durationUs);
  $("#cut-camera").disabled = !block || !state.sources[state.selectedSource] || block.logicalSourceId === state.sources[state.selectedSource].id;
  renderAudioInspector();
  renderIssues();
}

function renderAudioInspector() {
  const block = currentAudioBlock();
  if (!block) {
    $("#audio-meta").innerHTML = "<strong>No audio decision</strong><p>Add or initialize an audio block before rendering.</p>";
    return;
  }
  const value = block.mode === "FOLLOW_VIDEO" ? "follow"
    : block.mode === "SILENCE" ? "silence"
      : block.mode === "FIXED_CLIP" ? `clip:${block.clipId}`
        : `source:${block.logicalSourceId}`;
  $("#audio-source").value = value;
  $("#audio-offset").value = Math.round(Number(block.offsetUs || 0) / 1000);
  $("#link-av").classList.toggle("active", block.mode === "FOLLOW_VIDEO");
  $("#unlink-av").classList.toggle("active", block.mode !== "FOLLOW_VIDEO");
  $("#audio-meta").innerHTML = `<strong>${safe(block.mode)}</strong><p>Independent block ${formatUs(block.startUs)}–${formatUs(block.endUs)} · ${block.offsetUs >= 0 ? "+" : ""}${Math.round(block.offsetUs / 1000)} ms · ${block.ratePpm || 0} ppm</p>`;
  $("#segment-provenance").innerHTML = `<p class="eyebrow">Canonical decision</p><h2>${safe(currentVideoBlock()?.id || "No video block")}</h2><div class="summary"><div class="summary-row"><span>Output interval</span><strong>${formatUs(currentVideoBlock()?.startUs || 0)}–${formatUs(currentVideoBlock()?.endUs || 0)}</strong></div><div class="summary-row"><span>Audio decision</span><strong>${safe(block.mode)}</strong></div><div class="summary-row"><span>Project revision</span><strong>${state.project.revision}</strong></div></div>`;
}

function renderIssues() {
  const issues = state.compiled.issues || [];
  const first = issues[0];
  const lane = $("#reconciliation-lane");
  lane.classList.toggle("hidden", !issues.length);
  $("#program-status").className = `tag ${state.compiled.valid ? "valid" : "invalid"}`;
  $("#program-status").textContent = state.compiled.valid ? "✓ Canonical coverage valid" : `△ ${issues.length} backend issue${issues.length === 1 ? "" : "s"}`;
  if (!first) return;
  lane.innerHTML = `<strong>△ ${safe(first.code)}</strong> · ${formatUs(first.startUs)}–${formatUs(first.endUs)}<div>${safe(first.message)}</div>${first.code.startsWith("VIDEO_") && ["VIDEO_GAP", "VIDEO_OVERLAP"].includes(first.code) ? '<div class="issue-actions"><button id="repair-one">Preview and reconcile this boundary</button></div>' : ""}<div class="issue-list">${issues.slice(1, 5).map(issue => `<div class="issue-item">${safe(issue.code)} · ${safe(issue.message)}</div>`).join("")}</div>`;
  if ($("#repair-one")) $("#repair-one").onclick = () => reconcileIssue(first);
}

async function command(commandType, payload, {preview = false} = {}) {
  try {
    const result = await client.command(state.project.id, {
      commandId: crypto.randomUUID(),
      expectedRevision: state.project.revision,
      commandType,
      payload,
    }, preview);
    if (preview) return result;
    state.project = result.project;
    state.compiled = await client.program(state.project.id);
    state.renderPlan = null;
    deriveSources();
    renderSources();
    renderProgram();
    $("#footer-status").textContent = `${state.project.name} · revision ${state.project.revision} · backend-authoritative ${state.view}`;
    return result;
  } catch (error) {
    if (error instanceof APIError && error.code === "REVISION_CONFLICT") {
      await openProject({id: state.project.id});
      toast("Project changed in another tab; current revision reloaded");
      return null;
    }
    handleError(error);
    return null;
  }
}

async function reconcileIssue(issue) {
  const blocks = sortedVideoBlocks();
  const left = blocks.find(block => block.endUs === (issue.code === "VIDEO_GAP" ? issue.startUs : issue.endUs));
  const right = blocks.find(block => block.startUs === (issue.code === "VIDEO_GAP" ? issue.endUs : issue.startUs));
  if (!left || !right) return toast("This issue requires source assignment or clip pinning, not boundary reconciliation");
  const atUs = issue.code === "VIDEO_GAP" ? issue.startUs : issue.endUs;
  const payload = {
    operation: issue.code === "VIDEO_GAP" ? "CLOSE_GAP" : "TRIM_OVERLAP",
    leftBlockId: left.id,
    rightBlockId: right.id,
    atUs,
  };
  const preview = await command("ReconcileBoundary", payload, {preview: true});
  if (!preview) return;
  const remaining = preview.issues.filter(item => item.code.startsWith("VIDEO_"));
  if (!window.confirm(`Apply this scoped reconciliation? ${remaining.length} video issue(s) would remain.`)) return;
  await command("ReconcileBoundary", payload);
  toast("Selected boundary reconciled explicitly");
}

async function cutToSelected() {
  const block = sortedVideoBlocks().find(item => item.startUs < currentOutputUs() && currentOutputUs() < item.endUs);
  const source = state.sources[state.selectedSource];
  if (!block || !source) return toast("Move the playhead inside a video block and select a source");
  if (block.logicalSourceId === source.id) return;
  const result = await command("CutToSource", {blockId: block.id, atUs: currentOutputUs(), logicalSourceId: source.id});
  if (result) {
    state.selectedSegment = sortedVideoBlocks().findIndex(item => item.startUs === currentOutputUs());
    renderProgram();
    toast(`Cut to ${source.label} at ${formatUs(currentOutputUs())}`);
  }
}

async function ensureAudioSliceForVideo() {
  const video = currentVideoBlock();
  if (!video) return null;
  let audio = sortedAudioBlocks().find(block => block.startUs <= video.startUs && block.endUs > video.startUs);
  if (!audio) return null;
  if (audio.startUs < video.startUs) {
    await command("SplitAudioBlock", {blockId: audio.id, atUs: video.startUs});
    audio = sortedAudioBlocks().find(block => block.startUs === video.startUs);
  }
  if (audio && audio.endUs > video.endUs) {
    await command("SplitAudioBlock", {blockId: audio.id, atUs: video.endUs});
    audio = sortedAudioBlocks().find(block => block.startUs === video.startUs);
  }
  return audio;
}

async function setAudioDecision(value, offsetUs = null) {
  const block = await ensureAudioSliceForVideo();
  if (!block) return toast("No audio block covers the selected video interval");
  const payload = {
    blockId: block.id,
    mode: value === "follow" ? "FOLLOW_VIDEO" : value === "silence" ? "SILENCE" : value.startsWith("clip:") ? "FIXED_CLIP" : "FIXED_SOURCE",
    logicalSourceId: value.startsWith("source:") ? value.slice(7) : null,
    clipId: value.startsWith("clip:") ? value.slice(5) : null,
    offsetUs: offsetUs ?? Number(block.offsetUs || 0),
    ratePpm: Number(block.ratePpm || 0),
    confirmDrift: Boolean(block.ratePpm),
  };
  await command("SetAudioMode", payload);
}

async function setPlayhead(value) {
  state.playhead = Math.max(0, Math.min(100, Number(value)));
  $$(".playhead").forEach(node => { node.style.left = `${state.playhead}%`; });
  $(".scrubber").value = state.playhead;
  $("#align-time").textContent = formatUs(currentOutputUs());
  $("#align-clock").textContent = formatUs(currentOutputUs());
  $("#program-clock").textContent = `OUTPUT ${formatUs(currentOutputUs())}`;
  const index = sortedVideoBlocks().findIndex(block => block.startUs <= currentOutputUs() && currentOutputUs() < block.endUs);
  if (index >= 0) state.selectedSegment = index;
  renderProgram();
  clearTimeout(setPlayhead.pointTimer);
  setPlayhead.pointTimer = setTimeout(async () => {
    try {
      const point = await client.programAt(state.project.id, currentOutputUs());
      const source = sourceById(point.video?.logicalSourceId);
      $("#program-source").textContent = source?.label || (point.video ? "Selected asset" : "Output gap");
      $("#program-segment-id").textContent = point.video?.blockId || "—";
    } catch (_error) {
      // A later command refresh supplies authoritative state.
    }
  }, 120);
}

function togglePlay() {
  state.playing = !state.playing;
  $$('[data-action="play"]').forEach(button => { button.textContent = state.playing ? "Ⅱ" : "▶"; });
  clearInterval(state.timer);
  if (state.playing) {
    state.timer = setInterval(() => {
      setPlayhead(state.playhead + (10_000_000 / state.durationUs));
      if (state.playhead >= 100) togglePlay();
    }, 100);
  }
}

async function prepareReview() {
  state.renderPlan = null;
  state.artifact = null;
  $("#reviewed-check").checked = false;
  $("#render-video").disabled = true;
  $("#preflight-heading").textContent = "Building immutable render plan";
  $("#preflight-status").innerHTML = "<strong>Hashing selected sources</strong><p>Review will bind exact source bytes, decisions, transformations, destination, and warnings.</p>";
  try {
    const rawPath = $("#output-path").value.trim();
    const separator = rawPath.lastIndexOf("/");
    if (separator <= 0 || separator === rawPath.length - 1) throw new Error("Output must be an absolute file path");
    const directory = rawPath.slice(0, separator) || "/";
    let filename = rawPath.slice(separator + 1);
    const profile = $("#lossless-check").checked ? "ARCHIVAL_LOSSLESS" : "COMPATIBLE";
    const suffix = profile === "ARCHIVAL_LOSSLESS" ? ".mkv" : ".mp4";
    if (!filename.toLowerCase().endsWith(suffix)) throw new Error(`${profile} output filename must end with ${suffix}`);
    const grant = await client.createGrant({path: directory, role: "WRITE_OUTPUT"});
    state.renderPlan = await client.renderPlan(state.project.id, {outputGrantId: grant.id, filename, profile});
    renderReviewPlan();
  } catch (error) {
    $("#preflight-heading").textContent = "Output plan needs attention";
    $("#preflight-status").innerHTML = `<strong>△ Preflight unavailable</strong><p>${safe(error.message)}</p>`;
    $("#manifest-preview").textContent = "No immutable plan has been created.";
  }
}

function renderReviewPlan() {
  const plan = state.renderPlan;
  const blocking = plan.issues.filter(issue => issue.severity === "BLOCKING");
  $("#manifest-preview").textContent = JSON.stringify(plan, null, 2);
  $("#preflight-heading").textContent = plan.status === "READY" ? "Immutable output plan ready" : "Render blocked by canonical issues";
  $("#review-summary").innerHTML = [
    ["Project revision", plan.projectRevision],
    ["Output duration", formatUs(plan.compiledProgram.durationUs)],
    ["Compiled video slices", plan.compiledProgram.videoSlices.length],
    ["Compiled audio slices", plan.compiledProgram.audioSlices.length],
    ["Source identities", plan.sources.length],
    ["Profile", plan.profile],
    ["Blocking issues", blocking.length],
  ].map(([label, value]) => `<div class="summary-row"><span>${safe(label)}</span><strong>${safe(value)}</strong></div>`).join("");
  $("#preflight-status").innerHTML = plan.status === "READY"
    ? `<strong>✓ Exact plan ready for review</strong><p>Plan digest ${safe(plan.planDigest.slice(0, 16))}…</p>${plan.warningCodes.length ? `<div class="warning-list">${plan.warningCodes.map(code => `<div>${safe(code)}</div>`).join("")}</div>` : ""}`
    : `<strong>△ Render blocked</strong><div class="issue-list">${blocking.map(issue => `<div class="issue-item">${safe(issue.code)} · ${safe(issue.message)}</div>`).join("")}</div>`;
  updateRenderButton();
}

function updateRenderButton() {
  $("#render-video").disabled = !state.renderPlan || state.renderPlan.status !== "READY" || !$("#reviewed-check").checked;
}

async function renderVideo() {
  if (!state.renderPlan) return;
  try {
    $("#render-video").disabled = true;
    await client.attest(state.renderPlan.id, state.renderPlan.warningCodes);
    const result = await client.render(state.renderPlan.id, {});
    state.renderJob = result.job;
    state.artifact = result.artifact;
    const panel = $("#render-progress");
    panel.classList.remove("hidden");
    await pollJob(result.job.id, job => {
      panel.textContent = `${job.status} · ${job.message || ""} · ${Math.round((job.progress || 0) * 100)}%`;
    });
    const finalJob = await client.job(result.job.id);
    if (finalJob.status === "SUCCEEDED") {
      state.artifact = await client.artifact(result.artifact.id);
      $("#manifest-preview").textContent = JSON.stringify(await client.manifest(result.artifact.id), null, 2);
      toast("Video and provenance manifest completed as one artifact pair");
    } else {
      toast(`Render ${finalJob.status.toLowerCase()}: ${finalJob.message || "no detail"}`);
    }
  } catch (error) {
    handleError(error);
    updateRenderButton();
  }
}

async function pollJob(jobId, onUpdate) {
  while (true) {
    const job = await client.job(jobId);
    onUpdate(job);
    if (["SUCCEEDED", "FAILED", "CANCELED", "INTERRUPTED", "FAILED_RECOVERABLE"].includes(job.status)) return job;
    await new Promise(resolve => setTimeout(resolve, 500));
  }
}

async function scanLibrary(path, limit) {
  const grant = await client.createGrant({path, role: "READ_ONLY_SOURCE"});
  state.library = await client.createLibrary({sourceGrantId: grant.id, timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"});
  state.scanJob = await client.startScan(state.library.id, limit ? {mode: "BOUNDED", limit} : {mode: "FULL"});
  const panel = $("#scan-progress");
  panel.classList.remove("hidden");
  const scan = await pollJob(state.scanJob.id, job => {
    $("#scan-count").textContent = `${Math.round((job.progress || 0) * 100)}% · ${job.message || "Scanning"}`;
  });
  panel.classList.add("hidden");
  if (scan.status !== "SUCCEEDED") throw new Error(scan.message || `Scan ${scan.status.toLowerCase()}`);
  await loadLibraries();
  toast("Read-only scan complete; warnings and incomplete evidence remain inspectable");
}

function setupEvents() {
  $$('[data-view]').forEach(button => { button.onclick = () => showView(button.dataset.view); });
  $("#scan-form").onsubmit = async event => {
    event.preventDefault();
    try {
      const limit = $("#scan-limit").value;
      await scanLibrary($("#library-path").value.trim(), limit ? Number(limit) : null);
    } catch (error) {
      $("#scan-progress").classList.add("hidden");
      handleError(error);
    }
  };
  $("#load-more-media").onclick = () => loadMediaPage(false).catch(handleError);
  $("#open-event").onclick = createProjectFromGroup;
  $("#sync-offset").onchange = async event => {
    const source = state.sources[state.selectedSource];
    const clip = source?.clips[0];
    if (!clip) return;
    const sync = {...clip.sync, anchorOutputUs: Math.round(Number(event.target.value) * 1000)};
    const preview = await command("SetSyncTransform", {clipId: clip.id, sync, confirmDrift: Boolean(sync.ratePpm)}, {preview: true});
    if (!preview) return;
    const introduced = preview.issues.filter(issue => !state.compiled.issues.some(previous => previous.id === issue.id));
    if (introduced.length && !window.confirm(`This alignment introduces ${introduced.length} new canonical issue(s). Apply it?`)) return;
    await command("SetSyncTransform", {clipId: clip.id, sync, confirmDrift: Boolean(sync.ratePpm)});
  };
  $$('[data-nudge]').forEach(button => {
    button.onclick = () => {
      $("#sync-offset").value = Number($("#sync-offset").value) + Number(button.dataset.nudge);
      $("#sync-offset").dispatchEvent(new Event("change"));
    };
  });
  $("#set-reference").onclick = async () => {
    const source = state.sources[state.selectedSource];
    if (source && await command("SetReferenceSource", {sourceId: source.id})) toast(`${source.label} is the reference source`);
  };
  $$('input[name="anchor"]').forEach(radio => {
    radio.onchange = async () => {
      const anchorMode = radio.value === "source-clips" ? "SOURCE_TIME" : "PROGRAM_TIME";
      const preview = await command("SetAnchoringMode", {anchorMode}, {preview: true});
      if (preview && window.confirm(anchorMode === "SOURCE_TIME" ? "Attach future timing edits to source-relative points? Alignment changes may move output boundaries." : "Keep future program boundaries fixed on the output clock?")) {
        await command("SetAnchoringMode", {anchorMode});
      } else if (preview) {
        radio.checked = false;
        $$('input[name="anchor"]').find(item => item.value !== radio.value).checked = true;
      }
    };
  });
  $("#cut-camera").onclick = cutToSelected;
  $("#video-source").onchange = event => {
    const block = currentVideoBlock();
    if (block) command("AssignVideoSource", {blockId: block.id, logicalSourceId: event.target.value});
  };
  $("#link-av").onclick = () => setAudioDecision("follow");
  $("#unlink-av").onclick = () => {
    const sourceId = currentVideoBlock()?.logicalSourceId;
    if (sourceId) setAudioDecision(`source:${sourceId}`);
  };
  $("#audio-source").onchange = event => setAudioDecision(event.target.value);
  $("#audio-offset").onchange = event => {
    const block = currentAudioBlock();
    if (!block) return;
    const value = block.mode === "FOLLOW_VIDEO" ? "follow" : block.mode === "SILENCE" ? "silence" : block.mode === "FIXED_CLIP" ? `clip:${block.clipId}` : `source:${block.logicalSourceId}`;
    setAudioDecision(value, Math.round(Number(event.target.value) * 1000));
  };
  $$('[data-boundary]').forEach(button => {
    button.onclick = () => {
      const blocks = sortedVideoBlocks();
      const index = state.selectedSegment;
      if (index <= 0 || !blocks[index]) return toast("Select a video block after the first cut");
      const atUs = blocks[index].startUs + Math.round(Number(button.dataset.boundary) * 1_000_000);
      command("MoveVideoBoundary", {leftBlockId: blocks[index - 1].id, rightBlockId: blocks[index].id, atUs});
    };
  });
  $$('[data-action="play"]').forEach(button => { button.onclick = togglePlay; });
  $$('[data-action="rewind"]').forEach(button => { button.onclick = () => setPlayhead(state.playhead - (1_000_000_000 / state.durationUs)); });
  $$('[data-action="forward"]').forEach(button => { button.onclick = () => setPlayhead(state.playhead + (1_000_000_000 / state.durationUs)); });
  $(".scrubber").oninput = event => setPlayhead(Number(event.target.value));
  $("#reviewed-check").onchange = updateRenderButton;
  $("#render-video").onclick = renderVideo;
  $("#output-path").oninput = () => {
    clearTimeout(prepareReview.inputTimer);
    prepareReview.inputTimer = setTimeout(() => { if (state.view === "review") prepareReview(); }, 350);
  };
  $("#lossless-check").onchange = () => { if (state.view === "review") prepareReview(); };
  $("#download-manifest").onclick = async () => {
    const value = state.artifact?.status === "COMPLETE" ? await client.manifest(state.artifact.id) : state.renderPlan;
    if (!value) return toast("Create an immutable render plan first");
    const blob = new Blob([JSON.stringify(value, null, 2)], {type: "application/json"});
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `${state.project.name.replace(/\W+/g, "-").toLowerCase()}-${state.artifact ? "manifest" : "render-plan"}.json`;
    link.click();
    URL.revokeObjectURL(link.href);
  };
  document.addEventListener("keydown", event => {
    if (["INPUT", "SELECT", "TEXTAREA"].includes(event.target.tagName)) return;
    if (event.code === "Space") { event.preventDefault(); togglePlay(); }
    if (event.key.toLowerCase() === "c" && state.view === "cut") cutToSelected();
    if (event.key === "ArrowLeft" && event.altKey) setPlayhead(state.playhead - (10_000_000 / state.durationUs));
    if (event.key === "ArrowRight" && event.altKey) setPlayhead(state.playhead + (10_000_000 / state.durationUs));
  });
  $$('[data-inspector]').forEach(button => {
    button.onclick = () => {
      $$('[data-inspector]').forEach(item => item.classList.remove("active"));
      button.classList.add("active");
      $("#sync-panel").classList.toggle("hidden", button.dataset.inspector !== "sync");
      $("#provenance-panel").classList.toggle("hidden", button.dataset.inspector !== "provenance");
    };
  });
  $$('[data-cut-panel]').forEach(button => {
    button.onclick = () => {
      $$('[data-cut-panel]').forEach(item => item.classList.remove("active"));
      button.classList.add("active");
      $("#edit-panel").classList.toggle("hidden", button.dataset.cutPanel !== "edit");
      $("#segment-provenance").classList.toggle("hidden", button.dataset.cutPanel !== "prov");
    };
  });
}

function handleError(error) {
  const label = error instanceof APIError ? `${error.code}: ${error.message}` : error.message || String(error);
  toast(label);
}

async function start() {
  try {
    await client.session();
    setupEvents();
    await loadLibraries();
    $("#footer-status").textContent = "Secure local session ready";
  } catch (error) {
    $("#footer-status").textContent = "Secure launch required";
    handleError(error);
  }
}

start();
