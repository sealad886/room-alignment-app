const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const colors = ["#74c9bd", "#dda96a", "#8faed2", "#b58ccc", "#91bd7c", "#d28282"];
const MEDIA_TABLE_LIMIT = 500;
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
  presentedGroup: [],
  groupName: null,
  selectedMedia: new Set(),
  project: null,
  compiled: null,
  sources: [],
  selectedSource: 0,
  selectedClipId: null,
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
  eventFeed: null,
  eventFeedOpen: false,
  eventReconnectTimer: null,
  suggestions: [],
  outputGrantByDirectory: new Map(),
  eventReconnectAttempts: 0,
  reviewPreparationVersion: 0,
  reviewPreparationPromise: null,
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

function normalizeInteger(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.round(number) : fallback;
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

function selectedAlignmentClip(source = state.sources[state.selectedSource]) {
  const clip = source?.clips.find(item => item.id === state.selectedClipId) || source?.clips[0] || null;
  state.selectedClipId = clip?.id || null;
  return clip;
}

function invalidateReviewPreparation() {
  state.reviewPreparationVersion += 1;
  state.renderPlan = null;
  state.artifact = null;
  const reviewed = $("#reviewed-check");
  if (reviewed) reviewed.checked = false;
  const renderButton = $("#render-video");
  if (renderButton) renderButton.disabled = true;
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
  const [libraries, projects] = await Promise.all([client.listLibraries(), client.listProjects()]);
  state.projects = projects;
  renderRecentProjects();
  if (!libraries.length) return;
  state.library = libraries[0];
  syncLibraryControls();
  await loadMediaPage(true);
}

function syncLibraryControls() {
  if (!state.library) return;
  $("#library-time-zone").value = state.library.timeZone || "UTC";
  $("#library-dst-fold").value = String(state.library.dstFold || 0);
  $("#library-nonexistent").value = state.library.nonexistentPolicy || "REJECT";
}

async function loadMediaPage(reset = false) {
  if (!state.library) return;
  if (reset) {
    state.media = [];
    state.mediaById.clear();
    state.mediaCursor = null;
    state.mediaGeneration = null;
    state.groups = [];
    state.group = [];
    state.groupName = null;
    state.selectedMedia = new Set();
  }
  const query = new URLSearchParams({limit: "500"});
  if (state.mediaCursor) query.set("cursor", state.mediaCursor);
  if (state.mediaGeneration !== null) query.set("generation", String(state.mediaGeneration));
  const page = await client.listMedia({libraryId: state.library.id, query: Object.fromEntries(query)});
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
  state.presentedGroup = state.group.slice(0, MEDIA_TABLE_LIMIT);
  state.selectedMedia = new Set(state.presentedGroup.map(item => item.id));
  $("#media-table").innerHTML = state.presentedGroup.map(item => `
    <tr data-media="${safe(item.id)}">
      <td><input class="media-select" type="checkbox" value="${safe(item.id)}" checked aria-label="Include ${safe(mediaLabel(item))} clip"></td>
      <td class="mono">${safe(item.captured_at?.replace("T", " ") || "Unknown")}</td>
      <td>${safe(mediaLabel(item))}<br><small>${safe(item.relative_path)}</small></td>
      <td class="mono">${item.durationUs == null ? "Unknown" : formatUs(item.durationUs)}</td>
      <td>${safe(item.video_codec || "unsupported")}${item.width ? ` · ${item.width}×${item.height}` : ""}${item.audio_codec ? `<br><small>${safe(item.audio_codec)} · ${safe(item.sample_rate || "?")} Hz</small>` : "<br><small>No usable audio reported</small>"}</td>
      <td><span class="evidence-count">${item.evidence?.length || 0} observations</span>${item.warning ? `<br><small>△ ${safe(item.warning)}</small>` : ""}</td>
    </tr>`).join("");
  $$("#media-table tr").forEach(row => { row.ondblclick = createProjectFromGroup; });
  $("#select-group").checked = true;
  $("#select-group").indeterminate = false;
  $$(".media-select").forEach(input => {
    input.onchange = () => {
      if (input.checked) state.selectedMedia.add(input.value); else state.selectedMedia.delete(input.value);
      const count = state.presentedGroup.filter(item => state.selectedMedia.has(item.id)).length;
      $("#select-group").checked = count === state.presentedGroup.length;
      $("#select-group").indeterminate = count > 0 && count < state.presentedGroup.length;
      $("#open-event").disabled = count === 0;
    };
  });
  $("#event-title").textContent = state.group.length > state.presentedGroup.length
    ? `${state.groupName} · showing ${state.presentedGroup.length} of ${state.group.length} clips; unshown clips are excluded`
    : `${state.groupName} · ${state.group.length} clips`;
}

async function ensureProjectMedia(project) {
  const missingDetails = [...new Set(
    project.clips.map(clip => clip.assetId).filter(assetId => {
      const media = state.mediaById.get(assetId);
      return !media || !Array.isArray(media.resolutions);
    })
  )];
  for (let index = 0; index < missingDetails.length; index += 8) {
    const results = await Promise.allSettled(
      missingDetails.slice(index, index + 8).map(mediaId => client.getMedia({mediaId}))
    );
    for (const result of results) {
      if (result.status === "fulfilled") state.mediaById.set(result.value.id, result.value);
    }
  }
}

async function createProjectFromGroup() {
  const selected = state.group.filter(item => state.selectedMedia.has(item.id));
  if (!selected.length) return toast("Select at least one exact media asset");
  try {
    const project = await client.createProject({}, {
      name: `${state.groupName} alignment`,
      libraryId: state.library.id,
      assetIds: selected.map(item => item.id),
    });
    state.projects = await client.listProjects();
    renderRecentProjects();
    await openProject(project);
    toast("Draft program created; unresolved coverage remains visible");
  } catch (error) {
    handleError(error);
  }
}

async function openProject(projectSummary) {
  try {
    const project = await client.getProject({projectId: projectSummary.id});
    const libraries = await client.listLibraries();
    const projectLibrary = libraries.find(item => item.id === project.libraryId);
    if (!projectLibrary) throw new Error("Project library is unavailable");
    const libraryChanged = state.library?.id !== projectLibrary.id;
    state.library = projectLibrary;
    syncLibraryControls();
    if (libraryChanged) await loadMediaPage(true);
    await ensureProjectMedia(project);
    state.project = project;
    state.compiled = await client.getCompiledProgram({projectId: project.id});
    state.durationUs = Math.max(1, state.compiled.durationUs);
    state.selectedSource = 0;
    state.selectedClipId = null;
    state.selectedSegment = 0;
    invalidateReviewPreparation();
    state.suggestions = await client.listSuggestions({projectId: project.id});
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
  selectedAlignmentClip();
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
      const nextSource = Number(button.dataset.source);
      if (nextSource !== state.selectedSource) state.selectedClipId = null;
      state.selectedSource = nextSource;
      renderSources();
      renderSourceInspector();
      renderProgram();
    };
  });
  $$('#alignment-tracks [data-drag-source]').forEach(track => {
    track.onkeydown = event => {
      if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      event.preventDefault();
      const index = state.sources.findIndex(source => source.id === track.dataset.dragSource);
      if (index < 0) return;
      if (index !== state.selectedSource) state.selectedClipId = null;
      state.selectedSource = index;
      renderClipSelector(state.sources[index]);
      renderSourceInspector();
      const direction = event.key === "ArrowLeft" ? -1 : 1;
      $("#sync-offset").value = Number($("#sync-offset").value) + direction * (event.shiftKey ? 100 : 10);
      $("#sync-offset").dispatchEvent(new Event("change"));
    };
    track.onpointerdown = event => {
      const index = state.sources.findIndex(source => source.id === track.dataset.dragSource);
      if (index < 0) return;
      if (index !== state.selectedSource) state.selectedClipId = null;
      state.selectedSource = index;
      renderClipSelector(state.sources[index]);
      renderSourceInspector();
      const startX = event.clientX;
      const startMs = Number($("#sync-offset").value);
      const width = Math.max(1, track.querySelector(".track-clips").clientWidth);
      track.setPointerCapture(event.pointerId);
      track.onpointermove = moveEvent => {
        const deltaUs = ((moveEvent.clientX - startX) / width) * state.durationUs;
        $("#sync-offset").value = Math.round(startMs + deltaUs / 1000);
      };
      const finishPointer = upEvent => {
        if (track.hasPointerCapture(upEvent.pointerId)) track.releasePointerCapture(upEvent.pointerId);
        track.onpointermove = null;
        track.onpointerup = null;
        track.onpointercancel = null;
        $("#sync-offset").dispatchEvent(new Event("change"));
      };
      track.onpointerup = finishPointer;
      track.onpointercancel = finishPointer;
    };
  });
  const selected = state.sources[state.selectedSource];
  if (selected) {
    $("#source-label").value = selected.label;
    renderClipSelector(selected);
    $("#assign-source").innerHTML = state.sources.filter(source => source.id !== selected.id).map(source => `<option value="${safe(source.id)}">${safe(source.label)}</option>`).join("") || '<option value="">No other source</option>';
    $("#merge-source").innerHTML = $("#assign-source").innerHTML;
    $("#archive-source").disabled = Boolean(selected.clips.length || state.project.videoBlocks.some(block => block.logicalSourceId === selected.id) || state.project.audioBlocks.some(block => block.logicalSourceId === selected.id));
  }
  renderSourceInspector();
  renderSuggestions();
}

function renderClipSelector(source) {
  const clipOptions = source.clips.map((clip, index) => `<option value="${safe(clip.id)}">Clip ${index + 1} · ${safe(state.mediaById.get(clip.assetId)?.relative_path || clip.id)}</option>`).join("");
  $("#manage-clip").innerHTML = clipOptions || '<option value="">No clips assigned</option>';
  const selectedClip = selectedAlignmentClip(source);
  $("#manage-clip").value = selectedClip?.id || "";
  $("#manage-clip").onchange = event => {
    state.selectedClipId = event.target.value || null;
    renderSourceInspector();
  };
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
  return `<div class="track-row" data-drag-source="${safe(source.id)}" tabindex="0" aria-label="${safe(source.label)} alignment track; use arrow keys for 10 millisecond nudges"><div class="track-label"><strong>${safe(source.label)}</strong><small>${source.clips.length} source clips</small></div><div class="track-clips">${clips}</div></div>`;
}

function renderSourceInspector() {
  const source = state.sources[state.selectedSource];
  if (!source) return;
  const clip = selectedAlignmentClip(source);
  const sync = clip?.sync || {anchorOutputUs: 0, ratePpm: 0};
  const media = clip ? state.mediaById.get(clip.assetId) : null;
  $("#selected-source-name").textContent = source.label;
  $("#source-label").value = source.label;
  $("#sync-offset").value = Math.round(Number(sync.anchorOutputUs || 0) / 1000);
  $("#sync-rate").value = Number(sync.ratePpm || 0);
  $("#confidence-label").textContent = media?.captured_at
    ? `Selected clip · timestamp evidence available`
    : `Selected clip · manual timing required`;
  $("#provenance-panel").innerHTML = provenanceMarkup(media) + (media ? `<form id="provenance-correction"><p class="eyebrow">Revisioned correction</p><label class="field"><span>Field</span><input id="resolution-field" required maxlength="100" placeholder="capturedAt"></label><label class="field"><span>Resolved value</span><input id="resolution-value" required maxlength="500"></label><label class="field"><span>Rationale (optional)</span><input id="resolution-rationale" maxlength="500"></label><button class="btn wide" type="submit">Record correction</button></form>` : "");
  if ($("#provenance-correction")) $("#provenance-correction").onsubmit = event => recordProvenanceCorrection(event, media);
}

function renderSuggestions() {
  const container = $("#suggestion-list");
  const rows = state.suggestions.filter(item => item.kind === "ALIGNMENT").slice(0, 8);
  container.innerHTML = rows.length ? rows.map(item => `<div class="confidence"><strong>${safe(item.status)} · ${Math.round(Number(item.confidence || 0) * 100)}%</strong><p>${safe(item.evidence?.join("; ") || "No supporting evidence")}</p><small>${safe(item.algorithm)} v${safe(item.algorithmVersion)} · ${safe(item.limitations?.join("; ") || "No limitations recorded")}</small>${item.status === "PENDING" ? `<div class="link-row"><button class="btn" data-accept-suggestion="${safe(item.id)}">Accept</button><button class="btn" data-reject-suggestion="${safe(item.id)}">Reject</button></div>` : ""}</div>`).join("") : '<p class="muted">No alignment suggestions. Manual decisions remain authoritative.</p>';
  $$('[data-accept-suggestion]').forEach(button => { button.onclick = () => resolveSuggestion(button.dataset.acceptSuggestion, true); });
  $$('[data-reject-suggestion]').forEach(button => { button.onclick = () => resolveSuggestion(button.dataset.rejectSuggestion, false); });
}

async function resolveSuggestion(suggestionId, accept) {
  const suggestion = state.suggestions.find(item => item.id === suggestionId);
  if (!suggestion) return;
  const payload = accept ? {suggestionId, clipId: suggestion.clipId, sync: suggestion.sync, confirmDrift: Boolean(suggestion.sync?.ratePpm)} : {suggestionId};
  if (await command(accept ? "AcceptAlignmentSuggestion" : "RejectAlignmentSuggestion", payload)) {
    state.suggestions = await client.listSuggestions({projectId: state.project.id});
    renderSuggestions();
  }
}

async function recordProvenanceCorrection(event, media) {
  event.preventDefault();
  try {
    await client.resolveProvenance({mediaId: media.id}, {
      field: $("#resolution-field").value,
      resolution: {value: $("#resolution-value").value},
      rationale: $("#resolution-rationale").value || null,
    });
    const refreshed = await client.getMedia({mediaId: media.id});
    state.mediaById.set(media.id, refreshed);
    state.project = await client.getProject({projectId: state.project.id});
    state.compiled = await client.getCompiledProgram({projectId: state.project.id});
    invalidateReviewPreparation();
    deriveSources();
    renderSources();
    renderProgram();
    toast("Correction recorded without erasing raw evidence; review invalidated");
  } catch (error) { handleError(error); }
}

function provenanceMarkup(media) {
  if (!media) return "<p>Source metadata unavailable.</p>";
  const resolutions = media.resolutions || [];
  const resolutionMarkup = resolutions.length
    ? `<p class="eyebrow">Saved resolution ledger</p>${resolutions.map(item => `
      <div class="confidence">
        <strong>${safe(item.field)} · revision ${safe(item.revision)}</strong>
        <p class="mono">${safe(JSON.stringify(item.resolution))}</p>
        <small>Rationale: ${safe(item.rationale || "Not supplied")} · ${safe(item.actor)} · ${safe(item.createdAt)}</small>
      </div>`).join("")}`
    : '<p class="muted">No saved provenance resolutions.</p>';
  return `<p class="eyebrow">Inspectable source asset</p><h2>${safe(mediaLabel(media))}</h2>
    <div class="summary">
      <div class="summary-row"><span>Opaque asset ID</span><strong class="mono">${safe(media.id)}</strong></div>
      <div class="summary-row"><span>Library-relative path</span><strong>${safe(media.relative_path)}</strong></div>
      <div class="summary-row"><span>Captured evidence</span><strong>${safe(media.captured_at || "Unresolved")}</strong></div>
      <div class="summary-row"><span>Streams</span><strong>${media.streams?.length || 0}</strong></div>
      <div class="summary-row"><span>Evidence observations</span><strong>${media.evidence?.length || 0}</strong></div>
    </div>${resolutionMarkup}<p class="eyebrow">Retained raw evidence</p><pre>${safe(JSON.stringify(media.evidence || [], null, 2))}</pre>`;
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
  const pinnedSource = sourceForBlock(block);
  $("#video-clip-pin").innerHTML = `<option value="">Automatic unambiguous clip</option>${(pinnedSource?.clips || []).map((clip, index) => `<option value="${safe(clip.id)}">Clip ${index + 1}</option>`).join("")}`;
  $("#video-clip-pin").value = block?.pinnedClipId || "";
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
  const offsetUs = normalizeInteger(block.offsetUs);
  $("#audio-offset").value = Math.round(offsetUs / 1000);
  $("#link-av").classList.toggle("active", block.mode === "FOLLOW_VIDEO");
  $("#unlink-av").classList.toggle("active", block.mode !== "FOLLOW_VIDEO");
  $("#audio-meta").innerHTML = `<strong>${safe(block.mode)}</strong><p>Independent block ${formatUs(block.startUs)}–${formatUs(block.endUs)} · ${offsetUs >= 0 ? "+" : ""}${Math.round(offsetUs / 1000)} ms · ${block.ratePpm || 0} ppm</p>`;
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
    const result = await client.applyProjectCommand({
      projectId: state.project.id,
      query: preview ? {preview: "true"} : {},
    }, {
      commandId: crypto.randomUUID(),
      expectedRevision: state.project.revision,
      commandType,
      payload,
    });
    if (preview) return result;
    state.project = result.project;
    state.compiled = await client.getCompiledProgram({projectId: state.project.id});
    invalidateReviewPreparation();
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
    if (!await command("SplitAudioBlock", {blockId: audio.id, atUs: video.startUs})) return null;
    audio = sortedAudioBlocks().find(block => block.startUs === video.startUs);
  }
  if (audio && audio.endUs > video.endUs) {
    if (!await command("SplitAudioBlock", {blockId: audio.id, atUs: video.endUs})) return null;
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
    offsetUs: normalizeInteger(offsetUs ?? block.offsetUs),
    ratePpm: normalizeInteger(block.ratePpm),
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
      const point = await client.getProgramAt({projectId: state.project.id, query: {outputUs: currentOutputUs()}});
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

async function prepareReview({provisionGrant = false} = {}) {
  invalidateReviewPreparation();
  const request = {
    version: state.reviewPreparationVersion,
    provisionGrant,
    projectId: state.project.id,
    rawPath: $("#output-path").value.trim(),
    profile: $("#lossless-check").checked ? "ARCHIVAL_LOSSLESS" : "COMPATIBLE",
  };
  $("#preflight-heading").textContent = "Building immutable render plan";
  $("#preflight-status").innerHTML = "<strong>Hashing selected sources</strong><p>Review will bind exact source bytes, decisions, transformations, destination, and warnings.</p>";
  const predecessor = state.reviewPreparationPromise || Promise.resolve();
  const operation = predecessor.catch(() => {}).then(() => performReviewPreparation(request));
  let tracked;
  tracked = operation.finally(() => {
    if (state.reviewPreparationPromise === tracked) state.reviewPreparationPromise = null;
  });
  state.reviewPreparationPromise = tracked;
  return tracked;
}

async function performReviewPreparation(request) {
  if (request.version !== state.reviewPreparationVersion) return;
  try {
    const separator = request.rawPath.lastIndexOf("/");
    if (separator <= 0 || separator === request.rawPath.length - 1) throw new Error("Output must be an absolute file path");
    const directory = request.rawPath.slice(0, separator) || "/";
    const filename = request.rawPath.slice(separator + 1);
    const suffix = request.profile === "ARCHIVAL_LOSSLESS" ? ".mkv" : ".mp4";
    if (!filename.toLowerCase().endsWith(suffix)) throw new Error(`${request.profile} output filename must end with ${suffix}`);
    let grant = state.outputGrantByDirectory.get(directory);
    if (!grant || grant.revoked) {
      if (!request.provisionGrant) {
        throw new Error("Confirm the output path to grant write access to its directory");
      }
      grant = await client.createGrant({}, {path: directory, role: "WRITE_OUTPUT"});
      state.outputGrantByDirectory.set(directory, grant);
    }
    if (request.version !== state.reviewPreparationVersion) return;
    const plan = await client.createRenderPlan(
      {projectId: request.projectId},
      {outputGrantId: grant.id, filename, profile: request.profile},
    );
    if (request.version !== state.reviewPreparationVersion) return;
    state.renderPlan = plan;
    renderReviewPlan();
  } catch (error) {
    if (request.version !== state.reviewPreparationVersion) return;
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
  const plan = state.renderPlan;
  const preparationVersion = state.reviewPreparationVersion;
  if (!plan) return;
  try {
    $("#render-video").disabled = true;
    await client.attestReview({planId: plan.id}, {acknowledgedWarnings: plan.warningCodes});
    if (preparationVersion !== state.reviewPreparationVersion || state.renderPlan?.id !== plan.id) {
      throw new Error("Output settings changed while review was being recorded; review the current plan again");
    }
    const result = await client.startRender({planId: plan.id}, {});
    state.renderJob = result.job;
    state.artifact = result.artifact;
    const panel = $("#render-progress");
    panel.classList.remove("hidden");
    await pollJob(result.job.id, job => {
      panel.textContent = `${job.status} · ${job.message || ""} · ${Math.round((job.progress || 0) * 100)}%`;
    });
    const finalJob = await client.getJob({jobId: result.job.id});
    if (finalJob.status === "SUCCEEDED") {
      state.artifact = await client.getArtifact({artifactId: result.artifact.id});
      $("#manifest-preview").textContent = JSON.stringify(await client.getManifest({artifactId: result.artifact.id}), null, 2);
      $("#download-video").hidden = false;
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
  const terminal = new Set(["SUCCEEDED", "FAILED", "CANCELED", "INTERRUPTED", "FAILED_RECOVERABLE"]);
  return new Promise((resolve, reject) => {
    let finished = false;
    let polling = false;
    let consecutiveFailures = 0;
    const update = async () => {
      if (finished || polling) return;
      polling = true;
      try {
        const job = await client.getJob({jobId});
        consecutiveFailures = 0;
        onUpdate(job);
        if (terminal.has(job.status)) {
          finished = true;
          window.removeEventListener("room-alignment-job", onEvent);
          window.removeEventListener("room-alignment-event-reset", onReset);
          clearInterval(timer);
          resolve(job);
        }
      } catch (error) {
        consecutiveFailures += 1;
        if (consecutiveFailures >= 3) {
          finished = true;
          window.removeEventListener("room-alignment-job", onEvent);
          window.removeEventListener("room-alignment-event-reset", onReset);
          clearInterval(timer);
          reject(error);
        }
      } finally {
        polling = false;
      }
    };
    const onEvent = event => {
      if (event.detail.jobId === jobId) update();
    };
    const onReset = () => update();
    window.addEventListener("room-alignment-job", onEvent);
    window.addEventListener("room-alignment-event-reset", onReset);
    const timer = setInterval(update, state.eventFeedOpen ? 1500 : 750);
    update();
  });
}

async function connectEventFeed() {
  clearTimeout(state.eventReconnectTimer);
  if (state.eventFeed) state.eventFeed.close();
  try {
    const authorization = await client.createEventToken();
    const query = new URLSearchParams({token: authorization.token, after: String(state.eventCursor)});
    const feed = new EventSource(`/api/v1/events?${query}`);
    state.eventFeed = feed;
    feed.onopen = () => {
      state.eventFeedOpen = true;
      state.eventReconnectAttempts = 0;
    };
    feed.addEventListener("job", event => {
      const value = JSON.parse(event.data);
      state.eventCursor = Math.max(state.eventCursor, Number(value.sequence || event.lastEventId || 0));
      window.dispatchEvent(new CustomEvent("room-alignment-job", {detail: value}));
    });
    feed.addEventListener("reset", event => {
      const value = JSON.parse(event.data);
      state.eventCursor = Number(value.latestSequence || event.lastEventId || 0);
      window.dispatchEvent(new CustomEvent("room-alignment-event-reset", {detail: value}));
    });
    feed.onerror = () => {
      state.eventFeedOpen = false;
      feed.close();
      state.eventReconnectAttempts += 1;
      const delay = Math.min(30_000, 500 * (2 ** Math.min(state.eventReconnectAttempts, 6)));
      state.eventReconnectTimer = setTimeout(connectEventFeed, delay);
    };
  } catch (_error) {
    state.eventFeedOpen = false;
    state.eventReconnectAttempts += 1;
    const delay = Math.min(30_000, 500 * (2 ** Math.min(state.eventReconnectAttempts, 6)));
    state.eventReconnectTimer = setTimeout(connectEventFeed, delay);
  }
}

async function scanLibrary(path, limit) {
  const grant = await client.createGrant({}, {path, role: "READ_ONLY_SOURCE"});
  state.library = await client.createLibrary({}, {sourceGrantId: grant.id, timeZone: $("#library-time-zone").value || "UTC", dstFold: Number($("#library-dst-fold").value), nonexistentPolicy: $("#library-nonexistent").value});
  state.scanJob = await client.startScan({libraryId: state.library.id}, limit ? {mode: "BOUNDED", limit} : {mode: "FULL"});
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
  $("#apply-time-policy").onclick = async () => {
    if (!state.library) return toast("Index or open a library first");
    try {
      state.library = await client.updateLibraryTimePolicy({libraryId: state.library.id}, {timeZone: $("#library-time-zone").value, dstFold: Number($("#library-dst-fold").value), nonexistentPolicy: $("#library-nonexistent").value});
      await loadMediaPage(true);
      toast("Timestamp policy applied; raw evidence retained and suggestions invalidated");
    } catch (error) { handleError(error); }
  };
  $("#select-group").onchange = event => {
    state.selectedMedia = event.target.checked
      ? new Set(state.presentedGroup.map(item => item.id))
      : new Set();
    $$(".media-select").forEach(input => { input.checked = event.target.checked; });
    event.target.indeterminate = false;
    $("#open-event").disabled = !event.target.checked;
  };
  $("#open-event").onclick = createProjectFromGroup;
  $("#sync-offset").onchange = async event => {
    await applySelectedSync(Math.round(Number(event.target.value) * 1000), Number($("#sync-rate").value));
  };
  $("#sync-rate").onchange = async event => {
    const ratePpm = Math.round(Number(event.target.value));
    if (ratePpm && !window.confirm("Rate correction changes timing fidelity and will be disclosed in the manifest. Apply it?")) {
      renderSourceInspector();
      return;
    }
    await applySelectedSync(Math.round(Number($("#sync-offset").value) * 1000), ratePpm);
  };
  async function applySelectedSync(anchorOutputUs, ratePpm) {
    const source = state.sources[state.selectedSource];
    const clip = selectedAlignmentClip(source);
    if (!clip) return;
    const sync = {...clip.sync, anchorOutputUs, ratePpm};
    const preview = await command("SetSyncTransform", {clipId: clip.id, sync, confirmDrift: Boolean(sync.ratePpm)}, {preview: true});
    if (!preview) return;
    const introduced = preview.issues.filter(issue => !state.compiled.issues.some(previous => previous.id === issue.id));
    if (introduced.length && !window.confirm(`This alignment introduces ${introduced.length} new canonical issue(s). Apply it?`)) return;
    await command("SetSyncTransform", {clipId: clip.id, sync, confirmDrift: Boolean(sync.ratePpm)});
  }
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
  $("#add-source").onclick = async () => {
    const label = window.prompt("Label for the new logical source", "New source");
    if (label) await command("AddLogicalSource", {label});
  };
  $("#rename-source").onclick = async () => {
    const source = state.sources[state.selectedSource];
    if (source) await command("RenameLogicalSource", {sourceId: source.id, label: $("#source-label").value});
  };
  $("#assign-clip").onclick = async () => {
    if ($("#manage-clip").value && $("#assign-source").value) await command("AssignClip", {clipId: $("#manage-clip").value, logicalSourceId: $("#assign-source").value});
  };
  $("#split-source").onclick = async () => {
    const source = state.sources[state.selectedSource];
    if (source && $("#manage-clip").value && $("#split-source-label").value) await command("SplitLogicalSource", {sourceId: source.id, clipIds: [$("#manage-clip").value], label: $("#split-source-label").value});
  };
  $("#merge-source-action").onclick = async () => {
    const source = state.sources[state.selectedSource];
    const targetSourceId = $("#merge-source").value;
    if (source && targetSourceId && window.confirm(`Merge ${source.label} into the selected destination? This remains a revisioned project decision.`)) await command("MergeLogicalSources", {targetSourceId, sourceIds: [source.id]});
  };
  $("#archive-source").onclick = async () => {
    const source = state.sources[state.selectedSource];
    if (source) await command("ArchiveLogicalSource", {sourceId: source.id, archived: true});
  };
  $("#analyze-alignment").onclick = async () => {
    try {
      const job = await client.startAlignmentAnalysis({projectId: state.project.id}, {});
      await pollJob(job.id, value => { $("#footer-status").textContent = `${value.status} · ${value.message}`; });
      state.suggestions = await client.listSuggestions({projectId: state.project.id});
      renderSuggestions();
    } catch (error) { handleError(error); }
  };
  $$('input[name="anchor"]').forEach(radio => {
    radio.onchange = async () => {
      const anchorMode = radio.value === "source-clips" ? "SOURCE_TIME" : "PROGRAM_TIME";
      const preview = await command("SetAnchoringMode", {anchorMode}, {preview: true});
      if (preview && window.confirm(anchorMode === "SOURCE_TIME" ? "Attach future timing edits to source-relative points? Alignment changes may move output boundaries." : "Keep future program boundaries fixed on the output clock?")) {
        if (await command("SetAnchoringMode", {anchorMode})) return;
      }
      radio.checked = false;
      const currentValue = state.project.anchorMode === "SOURCE_TIME" ? "source-clips" : "wall-clock";
      const current = $$('input[name="anchor"]').find(item => item.value === currentValue);
      if (current) current.checked = true;
    };
  });
  $("#cut-camera").onclick = cutToSelected;
  $("#video-source").onchange = event => {
    const block = currentVideoBlock();
    if (block) command("AssignVideoSource", {blockId: block.id, logicalSourceId: event.target.value});
  };
  $("#video-clip-pin").onchange = event => {
    const block = currentVideoBlock();
    if (block) command("PinVideoClip", {blockId: block.id, clipId: event.target.value || null});
  };
  $("#split-video").onclick = () => {
    const block = currentVideoBlock();
    const atUs = currentOutputUs();
    if (!block || atUs <= block.startUs || atUs >= block.endUs) return toast("Move the playhead inside the selected video block");
    command("SplitVideoBlock", {blockId: block.id, atUs});
  };
  $("#delete-video").onclick = () => {
    const block = currentVideoBlock();
    if (block && window.confirm("Delete this video decision and expose the resulting coverage issue?")) command("DeleteVideoBlock", {blockId: block.id});
  };
  $("#fill-video-gap").onclick = () => {
    const issue = state.compiled.issues.find(item => item.code === "VIDEO_GAP");
    const source = state.sources[state.selectedSource];
    if (!issue || !source) return toast("No canonical video gap is available to fill");
    command("AddVideoBlock", {startUs: issue.startUs, endUs: issue.endUs, logicalSourceId: source.id});
  };
  $("#split-audio").onclick = () => {
    const block = currentAudioBlock();
    const atUs = currentOutputUs();
    if (!block || atUs <= block.startUs || atUs >= block.endUs) return toast("Move the playhead inside the current audio block");
    command("SplitAudioBlock", {blockId: block.id, atUs});
  };
  $("#delete-audio").onclick = () => {
    const block = currentAudioBlock();
    if (block && window.confirm("Delete this audio decision and expose the resulting issue?")) command("DeleteAudioBlock", {blockId: block.id});
  };
  $("#fill-audio-gap").onclick = () => {
    const issue = state.compiled.issues.find(item => item.code === "AUDIO_GAP");
    if (!issue) return toast("No canonical audio gap is available to fill");
    command("AddAudioBlock", {startUs: issue.startUs, endUs: issue.endUs, mode: "SILENCE"});
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
    invalidateReviewPreparation();
    $("#preflight-heading").textContent = "Output settings changed";
    $("#preflight-status").innerHTML = "<strong>Preparing a new immutable plan</strong><p>The prior plan can no longer authorize rendering.</p>";
    clearTimeout(prepareReview.inputTimer);
    prepareReview.inputTimer = setTimeout(() => { if (state.view === "review") prepareReview(); }, 350);
  };
  $("#output-path").onblur = () => {
    clearTimeout(prepareReview.inputTimer);
    if (state.view === "review") prepareReview({provisionGrant: true});
  };
  $("#lossless-check").onchange = () => { if (state.view === "review") prepareReview(); };
  $("#download-manifest").onclick = async () => {
    const value = state.artifact?.status === "COMPLETE" ? await client.getManifest({artifactId: state.artifact.id}) : state.renderPlan;
    if (!value) return toast("Create an immutable render plan first");
    const blob = new Blob([JSON.stringify(value, null, 2)], {type: "application/json"});
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `${state.project.name.replace(/\W+/g, "-").toLowerCase()}-${state.artifact ? "manifest" : "render-plan"}.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 0);
  };
  $("#download-video").onclick = () => {
    if (!state.artifact || state.artifact.status !== "COMPLETE") return toast("Complete the artifact pair first");
    window.location.assign(`/api/v1/artifacts/${encodeURIComponent(state.artifact.id)}/video`);
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
    await client.getSession();
    $("#library-time-zone").value = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    setupEvents();
    connectEventFeed();
    await loadLibraries();
    $("#footer-status").textContent = "Secure local session ready";
  } catch (error) {
    $("#footer-status").textContent = "Secure launch required";
    handleError(error);
  }
}

start();
