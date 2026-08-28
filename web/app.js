const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const colors = ["#74c9bd", "#dda96a", "#8faed2", "#b58ccc", "#91bd7c", "#d28282"];
const MEDIA_TABLE_LIMIT = 500;
const {RoomAlignmentAPIClient, APIError} = window.RoomAlignmentAPI;
const client = new RoomAlignmentAPIClient();

const state = {
  view: "library",
  settings: {
    overlapSearchExtensionUs: 30_000_000,
    textScalePercent: 100,
    colorScheme: "DARKROOM",
    renderVideoCodec: "H264_VIDEOTOOLBOX",
    renderResolution: "FULL_HD_1080P",
  },
  library: null,
  media: [],
  mediaById: new Map(),
  mediaCursor: null,
  mediaGeneration: null,
  projects: [],
  clusterGeneration: null,
  clusterFacets: {roots: [], sourceCandidates: []},
  sessions: [],
  sessionCursor: null,
  eventsBySession: new Map(),
  expandedSessions: new Set(),
  selectedSessions: new Set(),
  selectedEvents: new Set(),
  manualIncludeAssetIds: new Set(),
  manualExcludeAssetIds: new Set(),
  selectionPreview: null,
  selectionPreviewVersion: 0,
  inspectedCluster: null,
  inspectedMemberships: [],
  inspectedCursor: null,
  showingUnclustered: false,
  clusterJob: null,
  project: null,
  compiled: null,
  preparation: null,
  alignmentSummary: null,
  timelineWindow: null,
  proposalSets: [],
  alignmentQueueLimit: 50,
  sources: [],
  selectedSource: 0,
  selectedClipId: null,
  selectedSegment: 0,
  playhead: 30,
  playing: false,
  timer: null,
  durationUs: 1,
  evidenceStartUs: 0,
  evidenceEndUs: 1,
  timelineStartUs: 0,
  timelineEndUs: 1,
  timelineZoom: 0,
  monitorPoint: null,
  monitorPointRequestVersion: 0,
  monitorPointRequestPending: false,
  monitorPointQueuedUs: null,
  programDurationUs: 1,
  renderPlan: null,
  artifact: null,
  scanJob: null,
  scanDetail: null,
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

/**
 * Displays a temporary notification message.
 * @param {string} message - The message to display.
 */
function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.remove("show"), 3600);
}

/**
 * Applies text scaling and color scheme settings to the document.
 * @param {Object} settings - Appearance settings containing text scale and color scheme values.
 */
function applyAppearanceSettings(settings = state.settings) {
  const scale = Math.max(85, Math.min(140, Number(settings.textScalePercent || 100))) / 100;
  document.documentElement.dataset.theme = settings.colorScheme || "DARKROOM";
  document.documentElement.dataset.textSize = scale > 1.1 ? "LARGE" : "STANDARD";
  document.documentElement.style.setProperty("--text-scale", String(scale));
  document.documentElement.style.setProperty("--inverse-text-scale", String(1 / scale));
  document.querySelector('meta[name="color-scheme"]')?.setAttribute(
    "content",
    settings.colorScheme === "DAYLIGHT" ? "light" : "dark",
  );
}

/**
 * Populate the settings form with the current application settings.
 * @param {Object} [settings=state.settings] - The settings to display in the form.
 */
function populateSettingsForm(settings = state.settings) {
  $("#overlap-search-seconds").value = Math.round(Number(settings.overlapSearchExtensionUs) / 1_000_000);
  $("#text-scale").value = String(settings.textScalePercent);
  $("#color-scheme").value = settings.colorScheme;
  $("#render-video-codec").value = settings.renderVideoCodec;
  $("#render-resolution").value = settings.renderResolution;
}

/**
 * Previews the current text scale and color scheme settings.
 */
function previewSettingsForm() {
  applyAppearanceSettings({
    ...state.settings,
    textScalePercent: Number($("#text-scale").value),
    colorScheme: $("#color-scheme").value,
  });
}

/**
 * Closes the settings dialog and optionally reapplies the saved appearance settings.
 * @param {boolean} [restore=true] - Whether to restore the saved appearance settings before closing.
 */
function closeSettings({restore = true} = {}) {
  if (restore) applyAppearanceSettings(state.settings);
  $("#settings-dialog").close();
}

/**
 * Saves application settings and applies the updated appearance preferences.
 * @param {Event} event - The settings form submission event.
 */
async function saveSettings(event) {
  event.preventDefault();
  const saveState = $("#settings-save-state");
  saveState.textContent = "Saving…";
  try {
    const previousExtension = Number(state.settings.overlapSearchExtensionUs);
    state.settings = await client.updateApplicationSettings({}, {
      overlapSearchExtensionUs: Math.round(Number($("#overlap-search-seconds").value) * 1_000_000),
      textScalePercent: Number($("#text-scale").value),
      colorScheme: $("#color-scheme").value,
      renderVideoCodec: $("#render-video-codec").value,
      renderResolution: $("#render-resolution").value,
    });
    applyAppearanceSettings();
    $("#settings-dialog").close();
    if (Number(state.settings.overlapSearchExtensionUs) !== previousExtension && state.project) {
      state.proposalSets = await client.listAlignmentProposalSets({projectId: state.project.id});
      renderSuggestions();
    }
    toast("Settings saved locally");
  } catch (error) {
    saveState.textContent = "Could not save";
    applyAppearanceSettings(state.settings);
    handleError(error);
  }
}

/**
 * Prompts the user to confirm an action through the confirmation dialog.
 * @param {string} message - The message to display.
 * @param {Object} [options] - Dialog customization options.
 * @param {string} [options.title="Confirm change"] - The dialog title.
 * @param {string} [options.confirmLabel="Confirm"] - The label for the confirmation button.
 * @return {Promise<boolean>} `true` if the action is confirmed, `false` otherwise.
 */
function confirmAction(message, {title = "Confirm change", confirmLabel = "Confirm"} = {}) {
  const dialog = $("#confirmation-dialog");
  if (!dialog?.showModal) return Promise.resolve(false);
  if (dialog.open) dialog.close("cancel");
  $("#confirmation-title").textContent = title;
  $("#confirmation-message").textContent = message;
  $("#confirmation-accept").textContent = confirmLabel;
  return new Promise(resolve => {
    const finish = () => resolve(dialog.returnValue === "confirm");
    dialog.addEventListener("close", finish, {once: true});
    dialog.showModal();
  });
}

function formatUs(valueUs = 0) {
  const milliseconds = Math.round(Number(valueUs) / 1000);
  const hours = Math.floor(milliseconds / 3_600_000);
  const minutes = Math.floor(milliseconds / 60_000) % 60;
  const seconds = Math.floor(milliseconds / 1000) % 60;
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${String(Math.abs(milliseconds) % 1000).padStart(3, "0")}`;
}

function countLabel(count, singular, plural = `${singular}s`) {
  return `${Number(count).toLocaleString()} ${Number(count) === 1 ? singular : plural}`;
}

function normalizeInteger(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.round(number) : fallback;
}

function mediaLabel(media) {
  return media?.camera || media?.relative_path?.split("/").at(-2) || "Unlabelled source";
}

function currentOutputUs() {
  return Math.round((state.playhead / 100) * state.programDurationUs);
}

function currentAlignedUs() {
  const spanUs = Math.max(1, state.evidenceEndUs - state.evidenceStartUs);
  return state.evidenceStartUs + Math.round((state.playhead / 100) * spanUs);
}

function activeClockDurationUs() {
  return state.view === "align"
    ? Math.max(1, state.evidenceEndUs - state.evidenceStartUs)
    : state.programDurationUs;
}

function clipAlignment(clip) {
  if (clip?.alignment) return clip.alignment;
  const legacy = clip?.sync || {};
  return {
    anchorSourceUs: Number(legacy.anchorSourceUs || 0),
    anchorAlignedUs: Number(legacy.anchorOutputUs || 0),
    ratePpm: Number(legacy.ratePpm || 0),
  };
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

function sourceTimeSeconds(clip, alignedUs = currentAlignedUs()) {
  const alignment = clipAlignment(clip);
  const numerator = 1_000_000 + Number(alignment.ratePpm || 0);
  const deltaUs = alignedUs - Number(alignment.anchorAlignedUs || 0);
  return Math.max(0, (Number(alignment.anchorSourceUs || 0) + (deltaUs * 1_000_000) / numerator) / 1_000_000);
}

function syncSourceMonitors(shouldPlay = state.playing) {
  if (!state.project) return;
  const alignedUs = currentAlignedUs();
  const point = state.monitorPoint;
  if (!point || point.revision !== state.project?.revision || alignedUs < point.validFromAlignedUs || alignedUs >= point.validUntilAlignedUs) {
    $("#monitor-context-status").textContent = `Finding exact clips at ${formatUs(alignedUs)}…`;
    $$("#source-monitors .source-monitor").forEach(monitor => monitor.classList.add("syncing"));
    scheduleSourceMonitorPoint(alignedUs);
    return;
  }
  applySourceMonitorPoint(point, shouldPlay);
}

function seekMonitorVideo(video, shouldPlay) {
  const target = Number(video.dataset.targetSeconds || 0);
  if (video.readyState >= 1 && Number.isFinite(video.duration)) {
    const bounded = Math.min(target, Math.max(0, video.duration - 0.02));
    if (Math.abs(video.currentTime - bounded) > 0.12) {
      if (typeof video.fastSeek === "function") video.fastSeek(bounded);
      else video.currentTime = bounded;
    }
  }
  if (shouldPlay) video.play().catch(() => {}); else video.pause();
}

function applySourceMonitorPoint(point, shouldPlay = state.playing) {
  const alignedUs = currentAlignedUs();
  const pointsBySource = new Map(point.sources.map(item => [item.logicalSourceId, item]));
  const activeClipIds = new Set();
  $$("#source-monitors .source-monitor").forEach(monitor => {
    const source = sourceById(monitor.dataset.sourceId);
    const sourcePoint = pointsBySource.get(monitor.dataset.sourceId);
    const candidate = sourcePoint?.candidates?.[0] || null;
    const video = monitor.querySelector("video");
    const grounding = monitor.querySelector(".monitor-grounding");
    const timing = monitor.querySelector(".monitor-time");
    monitor.classList.remove("syncing");
    monitor.classList.toggle("no-coverage", !candidate);
    if (!candidate) {
      video?.pause();
      monitor.dataset.clipId = "";
      grounding.textContent = "No recorded clip at playhead";
      timing.textContent = `Timeline ${formatUs(alignedUs)} · intentionally blank`;
      monitor.setAttribute("aria-label", `${source?.label || "Source"}: no recorded clip at ${formatUs(alignedUs)}`);
      return;
    }
    activeClipIds.add(candidate.clipId);
    const clip = clipById(candidate.clipId);
    if (video.dataset.assetId !== candidate.assetId) {
      video.dataset.assetId = candidate.assetId;
      video.src = `/api/v1/media/${encodeURIComponent(candidate.assetId)}/preview`;
      video.load();
    }
    const sourceSeconds = sourceTimeSeconds(clip, alignedUs);
    video.dataset.targetSeconds = String(sourceSeconds);
    video.muted = source?.id !== state.sources[state.selectedSource]?.id;
    video.onloadedmetadata = () => seekMonitorVideo(video, state.playing);
    video.oncanplay = () => { if (state.playing) video.play().catch(() => {}); };
    seekMonitorVideo(video, shouldPlay);
    const filename = candidate.relativePath.split("/").at(-1) || candidate.assetId;
    grounding.textContent = sourcePoint.status === "AMBIGUOUS"
      ? `${sourcePoint.candidates.length} overlapping clips · ${filename}`
      : filename;
    timing.textContent = `Timeline ${formatUs(alignedUs)} · clip ${formatUs(Math.round(sourceSeconds * 1_000_000))}`;
    monitor.dataset.clipId = candidate.clipId;
    monitor.setAttribute("aria-label", `${source?.label || "Source"}: ${filename} at timeline ${formatUs(alignedUs)}`);
  });
  $$("#alignment-tracks [data-timeline-clip]").forEach(node => {
    node.classList.toggle("monitor-active", activeClipIds.has(node.dataset.timelineClip));
  });
  const playheadVisible = alignedUs >= state.timelineStartUs && alignedUs < state.timelineEndUs;
  $("#monitor-context-status").textContent = playheadVisible
    ? `${formatUs(alignedUs)} · matching timeline clips highlighted below`
    : `${formatUs(alignedUs)} · playhead is outside the visible timeline window`;
}

function scheduleSourceMonitorPoint(alignedUs = currentAlignedUs()) {
  state.monitorPointQueuedUs = alignedUs;
  if (state.monitorPointRequestPending) return;
  state.monitorPointRequestPending = true;
  const version = state.monitorPointRequestVersion;
  clearTimeout(scheduleSourceMonitorPoint.timer);
  scheduleSourceMonitorPoint.timer = setTimeout(async () => {
    const requestedAlignedUs = state.monitorPointQueuedUs;
    state.monitorPointQueuedUs = null;
    try {
      const point = await client.getAlignedSourcePoint({projectId: state.project.id, query: {alignedUs: Math.round(requestedAlignedUs)}});
      if (version !== state.monitorPointRequestVersion || point.revision !== state.project.revision) return;
      state.monitorPoint = point;
      applySourceMonitorPoint(point, state.playing);
    } catch (error) {
      if (version === state.monitorPointRequestVersion) handleError(error);
    } finally {
      state.monitorPointRequestPending = false;
      const currentUs = currentAlignedUs();
      const point = state.monitorPoint;
      if (
        state.project
        && (!point || point.revision !== state.project.revision || currentUs < point.validFromAlignedUs || currentUs >= point.validUntilAlignedUs)
      ) scheduleSourceMonitorPoint(currentUs);
    }
  }, 24);
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
  if (view === "cut" && !state.preparation?.canEnterCut) {
    toast("Review alignment and build the first cut before opening Cut");
    return;
  }
  if (view === "review" && !state.preparation?.canEnterReview) {
    toast("Resolve canonical program issues before Review");
    return;
  }
  state.view = view;
  state.durationUs = activeClockDurationUs();
  $$(".view").forEach(node => node.classList.toggle("active", node.id === `${view}-view`));
  $$(".workflow").forEach(node => node.classList.toggle("active", node.dataset.view === view));
  if (view === "review") prepareReview();
  $("#footer-status").textContent = view === "library"
    ? "Library ready"
    : `${state.project.name} · revision ${state.project.revision} · backend-authoritative ${view}`;
}

function syncWorkflowAvailability() {
  const availability = {
    library: false,
    align: !state.project,
    cut: !state.preparation?.canEnterCut,
    review: !state.preparation?.canEnterReview,
  };
  $$(".workflow").forEach(button => {
    button.disabled = Boolean(availability[button.dataset.view]);
  });
  const continueCut = $("#continue-cut");
  if (continueCut) continueCut.disabled = !state.preparation?.canEnterCut;
}

async function refreshPreparationState() {
  if (!state.project) return;
  const [compiled, preparation, proposalSets] = await Promise.all([
    client.getCompiledProgram({projectId: state.project.id}),
    client.getProjectPreparation({projectId: state.project.id}),
    client.listAlignmentProposalSets({projectId: state.project.id}),
  ]);
  state.compiled = compiled;
  state.preparation = preparation;
  state.alignmentSummary = preparation.alignment;
  state.proposalSets = proposalSets;
  state.monitorPoint = null;
  state.monitorPointRequestVersion += 1;
  state.programDurationUs = Math.max(1, Number(compiled.durationUs || 0));
  const extent = preparation.alignment.evidenceSpan;
  state.evidenceStartUs = Number(extent.startAlignedUs || 0);
  state.evidenceEndUs = Math.max(state.evidenceStartUs + 1, Number(extent.endAlignedUs || 0));
  const previousCenterUs = (state.timelineStartUs + state.timelineEndUs) / 2;
  setTimelineViewport(state.timelineZoom, Number.isFinite(previousCenterUs) ? previousCenterUs : state.evidenceStartUs);
  await loadTimelineWindow();
  state.durationUs = activeClockDurationUs();
  syncWorkflowAvailability();
}

function setTimelineViewport(zoom = state.timelineZoom, centerUs = currentAlignedUs()) {
  const fullSpanUs = Math.max(1, state.evidenceEndUs - state.evidenceStartUs);
  state.timelineZoom = Math.max(0, Math.min(100, Number(zoom) || 0));
  const minimumSpanUs = Math.min(fullSpanUs, 1_000_000);
  const visibleSpanUs = Math.max(minimumSpanUs, fullSpanUs / Math.pow(2, state.timelineZoom / 20));
  const boundedCenterUs = Math.max(state.evidenceStartUs + visibleSpanUs / 2, Math.min(state.evidenceEndUs - visibleSpanUs / 2, centerUs));
  state.timelineStartUs = Math.round(boundedCenterUs - visibleSpanUs / 2);
  state.timelineEndUs = Math.round(boundedCenterUs + visibleSpanUs / 2);
}

async function loadTimelineWindow() {
  const spanUs = Math.max(1, state.timelineEndUs - state.timelineStartUs);
  state.timelineWindow = await client.getTimelineWindow({
    projectId: state.project.id,
    query: {
      startAlignedUs: state.timelineStartUs,
      endAlignedUs: state.timelineEndUs,
      resolutionUs: Math.max(1, Math.ceil(spanUs / 1600)),
    },
  });
  renderTimelineControls();
}

function renderTimelineControls() {
  const slider = $("#timeline-zoom");
  if (!slider) return;
  slider.value = state.timelineZoom;
  const fullSpanUs = Math.max(1, state.evidenceEndUs - state.evidenceStartUs);
  const visibleSpanUs = Math.max(1, state.timelineEndUs - state.timelineStartUs);
  $("#timeline-window-label").textContent = visibleSpanUs >= fullSpanUs - 1
    ? `Full span · ${formatUs(fullSpanUs)}`
    : `${formatUs(state.timelineStartUs)} – ${formatUs(state.timelineEndUs)} · ${formatUs(visibleSpanUs)} visible`;
  $("#timeline-zoom-out").disabled = state.timelineZoom <= 0;
  $("#timeline-zoom-in").disabled = state.timelineZoom >= 100;
  const pan = $("#timeline-pan");
  if (pan) {
    const availableTravelUs = Math.max(0, fullSpanUs - visibleSpanUs);
    const traveledUs = Math.max(0, state.timelineStartUs - state.evidenceStartUs);
    pan.value = availableTravelUs ? Math.round((traveledUs / availableTravelUs) * 1000) : 0;
    pan.disabled = availableTravelUs <= 0;
    $("#timeline-pan-position").textContent = availableTravelUs <= 0
      ? "Full span visible"
      : `${Math.round((Number(pan.value) / 1000) * 100)}% across`;
  }
}

async function changeTimelineZoom(zoom, centerUs = currentAlignedUs()) {
  setTimelineViewport(zoom, centerUs);
  await loadTimelineWindow();
  renderTimelineTracks();
  updatePlayheadPositions();
}

async function panTimeline(fraction) {
  const fullSpanUs = Math.max(1, state.evidenceEndUs - state.evidenceStartUs);
  const visibleSpanUs = Math.max(1, state.timelineEndUs - state.timelineStartUs);
  const availableTravelUs = Math.max(0, fullSpanUs - visibleSpanUs);
  if (!availableTravelUs) return;
  const centerUs = state.evidenceStartUs + visibleSpanUs / 2 + Math.max(0, Math.min(1, fraction)) * availableTravelUs;
  setTimelineViewport(state.timelineZoom, centerUs);
  await loadTimelineWindow();
  renderTimelineTracks();
  updatePlayheadPositions();
}

function queueTimelinePan(fraction) {
  const boundedFraction = Math.max(0, Math.min(1, fraction));
  const pan = $("#timeline-pan");
  if (pan) {
    pan.value = Math.round(boundedFraction * 1000);
    $("#timeline-pan-position").textContent = `${Math.round(boundedFraction * 100)}% across`;
  }
  clearTimeout(queueTimelinePan.timer);
  queueTimelinePan.timer = setTimeout(() => panTimeline(boundedFraction).catch(handleError), 60);
}

async function loadLibraries() {
  const [libraries, projects] = await Promise.all([client.listLibraries(), client.listProjects()]);
  state.projects = projects;
  renderRecentProjects();
  if (!libraries.length) return;
  state.library = libraries[0];
  syncLibraryControls();
  await loadMediaPage(true);
  await loadClusterGeneration();
}

function syncLibraryControls() {
  if (!state.library) return;
  $("#library-name").value = state.library.name || "Video library";
  $("#library-name").disabled = true;
  $("#library-time-zone").value = state.library.timeZone || "UTC";
  $("#library-dst-fold").value = String(state.library.dstFold || 0);
  $("#library-nonexistent").value = state.library.nonexistentPolicy || "REJECT";
  $("#add-folder").textContent = "Add folder & scan it";
  renderLibraryRoots();
}

function renderLibraryRoots() {
  const panel = $("#library-roots-panel");
  const roots = state.library?.roots || [];
  panel.classList.toggle("hidden", !state.library);
  $("#root-count").textContent = `${roots.filter(root => root.active).length} / 16`;
  $("#scan-all-folders").disabled = !roots.some(root => root.active) || Boolean(state.scanJob);
  $("#library-roots").innerHTML = roots.map(root => {
    const progress = state.scanDetail?.roots?.find(item => item.rootId === root.id);
    const activity = progress
      ? `${progress.status.toLocaleLowerCase()} · ${Number(progress.scanned).toLocaleString()} inspected${progress.warnings ? ` · ${progress.warnings} warnings` : ""}`
      : (root.lastScanAt ? `scanned ${new Date(root.lastScanAt).toLocaleString()}` : "not yet scanned");
    return `
    <article class="library-root${root.active ? "" : " revoked"}">
      <div><strong>${safe(root.label)}</strong><small><span class="root-state">${root.active ? "Ready" : "Disconnected"}</span> · ${safe(activity)}</small></div>
      <div class="root-actions">${root.active ? `<button class="btn" type="button" data-scan-root="${safe(root.id)}"${state.scanJob ? " disabled" : ""}>Scan</button><button class="btn" type="button" data-revoke-root="${safe(root.id)}"${state.scanJob ? " disabled" : ""}>Disconnect</button>` : ""}</div>
    </article>`;
  }).join("") || '<p class="muted">No folders added yet.</p>';
  $$('[data-scan-root]').forEach(button => {
    button.onclick = () => scanRoots([button.dataset.scanRoot]).catch(handleError);
  });
  $$('[data-revoke-root]').forEach(button => {
    button.onclick = async () => {
      const root = roots.find(item => item.id === button.dataset.revokeRoot);
      if (!root || !await confirmAction(`Disconnect “${root.label}”? Indexed records and project decisions will remain, but its media will be unavailable.`, {title: "Disconnect library folder", confirmLabel: "Disconnect"})) return;
      try {
        await client.revokeLibraryRoot({libraryId: state.library.id, rootId: root.id}, {});
        await refreshCurrentLibrary();
        await loadMediaPage(true);
        await loadClusterGeneration();
        toast("Folder disconnected; indexed records and decisions were preserved");
      } catch (error) { handleError(error); }
    };
  });
}

async function refreshCurrentLibrary() {
  const libraries = await client.listLibraries();
  const current = libraries.find(item => item.id === state.library?.id) || libraries[0] || null;
  state.library = current;
  if (current) syncLibraryControls();
  return current;
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
  const page = await client.listMedia({libraryId: state.library.id, query: Object.fromEntries(query)});
  state.mediaGeneration = page.snapshotGeneration;
  state.mediaCursor = page.nextCursor;
  for (const media of page.items) {
    if (!state.mediaById.has(media.id)) state.media.push(media);
    state.mediaById.set(media.id, media);
  }
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
}

function resetClusterState() {
  state.clusterGeneration = null;
  state.clusterFacets = {roots: [], sourceCandidates: []};
  state.sessions = [];
  state.sessionCursor = null;
  state.eventsBySession = new Map();
  state.expandedSessions = new Set();
  state.selectedSessions = new Set();
  state.selectedEvents = new Set();
  state.manualIncludeAssetIds = new Set();
  state.manualExcludeAssetIds = new Set();
  state.selectionPreview = null;
  state.inspectedCluster = null;
  state.inspectedMemberships = [];
  state.inspectedCursor = null;
  state.showingUnclustered = false;
}

async function loadClusterGeneration() {
  resetClusterState();
  if (!state.library || state.library.catalogRevision < 1) return renderSessionExplorer();
  const page = await client.listClusterGenerations({
    libraryId: state.library.id,
    query: {limit: 50},
  });
  state.clusterGeneration = page.items.find(item =>
    item.status === "SUCCEEDED" && item.catalogRevision === state.library.catalogRevision
  ) || null;
  if (!state.clusterGeneration) return renderSessionExplorer();
  state.clusterFacets = await client.getClusterFacets({
    clusterGenerationId: state.clusterGeneration.id,
  });
  renderClusterFilters();
  await loadSessions(true);
}

function clusterFilterQuery() {
  const query = {limit: 100};
  const rootId = $("#session-root-filter").value;
  const sourceCandidateId = $("#session-source-filter").value;
  if (rootId) query.rootId = rootId;
  if (sourceCandidateId) query.sourceCandidateId = sourceCandidateId;
  if ($("#session-warning-filter").checked) query.warning = true;
  const date = $("#session-date-filter").value;
  if (date) {
    const start = new Date(`${date}T00:00:00`);
    const end = new Date(start);
    end.setDate(end.getDate() + 1);
    query.startUs = start.getTime() * 1000;
    query.endUs = end.getTime() * 1000;
  }
  return query;
}

function renderClusterFilters() {
  const roots = state.clusterFacets.roots || [];
  const candidates = state.clusterFacets.sourceCandidates || [];
  $("#session-root-filter").innerHTML = `<option value="">All folders</option>${roots.map(item =>
    `<option value="${safe(item.id)}">${safe(item.label)} · ${Number(item.clipCount).toLocaleString()}</option>`
  ).join("")}`;
  $("#session-source-filter").innerHTML = `<option value="">All candidates</option>${candidates.map(item =>
    `<option value="${safe(item.id)}">${safe(item.label)} · ${Number(item.clipCount).toLocaleString()}</option>`
  ).join("")}`;
}

async function loadSessions(reset = false) {
  if (!state.clusterGeneration) return renderSessionExplorer();
  if (reset) {
    state.sessions = [];
    state.sessionCursor = null;
    state.eventsBySession = new Map();
    state.expandedSessions = new Set();
  }
  const query = clusterFilterQuery();
  if (state.sessionCursor) query.cursor = state.sessionCursor;
  const page = await client.listSessionClusters({
    clusterGenerationId: state.clusterGeneration.id,
    query,
  });
  state.sessions.push(...page.items);
  state.sessionCursor = page.nextCursor;
  renderSessionExplorer();
}

function clusterTimeLabel(item) {
  const start = new Date(Number(item.startUs) / 1000);
  const end = new Date(Number(item.endUs) / 1000);
  const date = start.toLocaleDateString([], {year: "numeric", month: "short", day: "numeric"});
  const time = value => value.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
  return `${date} · ${time(start)}–${time(end)}`;
}

function renderSessionExplorer() {
  const status = $("#cluster-status");
  $("#generate-clusters").disabled = !state.library?.catalogRevision || Boolean(state.clusterJob);
  $("#show-unclustered").disabled = !state.clusterGeneration;
  if (!state.clusterGeneration) {
    status.textContent = state.library?.catalogRevision
      ? "No session map matches the latest catalog scan"
      : "Complete a scan before grouping media";
    $("#session-explorer").innerHTML = '<div class="empty-compact">Group the latest catalog into immutable sessions and events.</div>';
    $("#load-more-sessions").disabled = true;
    renderSelectionTray();
    return;
  }
  status.textContent = `${countLabel(state.clusterGeneration.sessionCount, "session")} · ${countLabel(state.clusterGeneration.eventCount, "event")} · catalog revision ${state.clusterGeneration.catalogRevision}`;
  $("#event-gap").value = String(state.clusterGeneration.config.eventGapUs / 1_000_000);
  $("#session-gap").value = String(state.clusterGeneration.config.sessionGapUs / 1_000_000);
  $("#session-explorer").innerHTML = state.sessions.map(session => {
    const expanded = state.expandedSessions.has(session.id);
    const selected = state.selectedSessions.has(session.id);
    const eventPage = state.eventsBySession.get(session.id);
    const eventRows = expanded ? (eventPage?.items || []).map(event => {
      const eventSelected = state.selectedEvents.has(event.id);
      return `<div class="event-row${eventSelected ? " selected" : ""}" data-inspect-kind="EVENT" data-inspect-id="${safe(event.id)}" data-parent-session="${safe(session.id)}">
        <span></span><input class="event-select" data-event-id="${safe(event.id)}" data-session-id="${safe(session.id)}" type="checkbox" ${eventSelected ? "checked" : ""} ${selected ? "disabled" : ""} aria-label="Select event ${safe(clusterTimeLabel(event))}">
        <button class="cluster-copy" data-inspect-kind="EVENT" data-inspect-id="${safe(event.id)}" data-parent-session="${safe(session.id)}"><strong>${safe(clusterTimeLabel(event))}</strong><small>${countLabel(event.clipCount, "clip")} · ${countLabel(event.sourceCount, "source candidate")} · ${countLabel(event.rootCount, "folder")}</small></button>
        <span class="cluster-metrics${event.warnings.length ? " cluster-warning" : ""}">${event.warnings.length ? `△ ${event.warnings.length} warning types` : "timed event"}</span>
      </div>`;
    }).join("") : "";
    const moreEvents = expanded && eventPage?.nextCursor
      ? `<button class="load-more-events" data-more-events="${safe(session.id)}">Load more events in this session</button>`
      : "";
    return `<div class="session-group"><div class="session-row${selected ? " selected" : ""}">
      <button class="cluster-expand" data-expand-session="${safe(session.id)}" aria-expanded="${expanded}" aria-label="${expanded ? "Collapse" : "Expand"} session">${expanded ? "▾" : "▸"}</button>
      <input class="session-select" data-session-id="${safe(session.id)}" type="checkbox" ${selected ? "checked" : ""} aria-label="Select session ${safe(clusterTimeLabel(session))}">
      <button class="cluster-copy" data-inspect-kind="SESSION" data-inspect-id="${safe(session.id)}"><strong>${safe(clusterTimeLabel(session))}</strong><small>${countLabel(session.eventCount, "event")} · ${countLabel(session.clipCount, "clip")} · ${countLabel(session.sourceCount, "source candidate")}</small></button>
      <span class="cluster-metrics${session.warnings.length ? " cluster-warning" : ""}">${session.rootCount} folder${session.rootCount === 1 ? "" : "s"}<br>${session.warnings.length ? `△ ${session.warnings.length} warning types` : "ready to inspect"}</span>
    </div>${eventRows}${moreEvents}</div>`;
  }).join("") || '<div class="empty-compact">No sessions match current filters.</div>';
  $("#load-more-sessions").disabled = !state.sessionCursor;
  $("#load-more-sessions").textContent = state.sessionCursor ? "Load more sessions" : "All sessions loaded";
  $$('[data-expand-session]').forEach(button => { button.onclick = () => toggleSession(button.dataset.expandSession); });
  $$(".session-select").forEach(input => { input.onchange = () => selectSession(input.dataset.sessionId, input.checked); });
  $$(".event-select").forEach(input => { input.onchange = () => selectEvent(input.dataset.eventId, input.checked); });
  $$('[data-more-events]').forEach(button => {
    button.onclick = () => loadMoreSessionEvents(button.dataset.moreEvents).catch(handleError);
  });
  $$('[data-inspect-kind]').filter(node => node.matches("button.cluster-copy")).forEach(button => {
    button.onclick = () => inspectCluster(button.dataset.inspectKind, button.dataset.inspectId, button.dataset.parentSession || null);
  });
  renderSelectionTray();
}

async function toggleSession(sessionId) {
  if (state.expandedSessions.has(sessionId)) {
    state.expandedSessions.delete(sessionId);
    return renderSessionExplorer();
  }
  state.expandedSessions.add(sessionId);
  if (!state.eventsBySession.has(sessionId)) {
    const page = await client.listEventClusters({
      clusterGenerationId: state.clusterGeneration.id,
      query: {...clusterFilterQuery(), sessionId, limit: 500},
    });
    state.eventsBySession.set(sessionId, page);
  }
  renderSessionExplorer();
}

async function loadMoreSessionEvents(sessionId) {
  const current = state.eventsBySession.get(sessionId);
  if (!current?.nextCursor) return;
  const page = await client.listEventClusters({
    clusterGenerationId: state.clusterGeneration.id,
    query: {
      ...clusterFilterQuery(),
      sessionId,
      limit: 500,
      cursor: current.nextCursor,
    },
  });
  state.eventsBySession.set(sessionId, {
    ...page,
    items: [...current.items, ...page.items],
  });
  renderSessionExplorer();
}

function selectSession(sessionId, selected) {
  if (selected) {
    state.selectedSessions.add(sessionId);
    for (const event of state.eventsBySession.get(sessionId)?.items || []) state.selectedEvents.delete(event.id);
  } else state.selectedSessions.delete(sessionId);
  renderSessionExplorer();
  refreshSelectionPreview();
}

function selectEvent(eventId, selected) {
  if (selected) state.selectedEvents.add(eventId); else state.selectedEvents.delete(eventId);
  renderSessionExplorer();
  refreshSelectionPreview();
}

async function inspectCluster(kind, clusterId, parentSessionId = null, append = false) {
  if (!append) {
    state.inspectedMemberships = [];
    state.inspectedCursor = null;
  }
  state.showingUnclustered = kind === "UNCLUSTERED";
  state.inspectedCluster = {kind, id: clusterId, parentSessionId};
  const query = {limit: MEDIA_TABLE_LIMIT};
  if (state.inspectedCursor) query.cursor = state.inspectedCursor;
  const page = kind === "SESSION"
    ? await client.listSessionMemberships({clusterId, query})
    : kind === "EVENT"
      ? await client.listEventMemberships({clusterId, query})
      : await client.listUnclusteredMemberships({clusterGenerationId: state.clusterGeneration.id, query});
  state.inspectedMemberships.push(...page.items);
  state.inspectedCursor = page.nextCursor;
  for (const item of page.items) state.mediaById.set(item.media.id, item.media);
  renderMemberships();
}

function inspectedClusterSelected() {
  const cluster = state.inspectedCluster;
  if (!cluster) return false;
  if (cluster.kind === "SESSION") return state.selectedSessions.has(cluster.id);
  if (cluster.kind === "EVENT") {
    return state.selectedEvents.has(cluster.id) || state.selectedSessions.has(cluster.parentSessionId);
  }
  return false;
}

function renderMemberships() {
  const cluster = state.inspectedCluster;
  $("#membership-title").textContent = cluster
    ? `${cluster.kind === "UNCLUSTERED" ? "Unclustered media" : `${cluster.kind.toLocaleLowerCase()} membership`} · ${state.inspectedMemberships.length.toLocaleString()} shown`
    : "Choose a session or event to inspect";
  const baseSelected = inspectedClusterSelected();
  $("#media-table").innerHTML = state.inspectedMemberships.map(item => {
    const media = item.media;
    const included = state.manualIncludeAssetIds.has(item.assetId) || (baseSelected && !state.manualExcludeAssetIds.has(item.assetId));
    return `<tr data-media="${safe(item.assetId)}">
      <td><input class="media-use" type="checkbox" value="${safe(item.assetId)}" ${included ? "checked" : ""} aria-label="Use ${safe(mediaLabel(media))} clip"></td>
      <td class="mono">${safe(media.captured_at?.replace("T", " ") || "Unknown")}</td>
      <td>${safe(mediaLabel(media))}<br><small>${safe(media.relative_path)}</small></td>
      <td class="mono">${media.durationUs == null ? "Unknown" : formatUs(media.durationUs)}</td>
      <td>${safe(media.video_codec || "unsupported")}${media.width ? ` · ${media.width}×${media.height}` : ""}${media.audio_codec ? `<br><small>${safe(media.audio_codec)} · ${safe(media.sample_rate || "?")} Hz</small>` : "<br><small>No usable audio reported</small>"}</td>
      <td><span class="evidence-count">${media.evidence?.length || 0} observations</span>${item.warnings?.length ? `<br><small>△ ${safe(item.warnings.join(", "))}</small>` : ""}</td>
    </tr>`;
  }).join("") || '<tr><td colspan="6" class="muted">No media in this view.</td></tr>';
  $$(".media-use").forEach(input => {
    input.onchange = () => {
      if (input.checked) {
        state.manualExcludeAssetIds.delete(input.value);
        if (!baseSelected) state.manualIncludeAssetIds.add(input.value);
      } else {
        state.manualIncludeAssetIds.delete(input.value);
        if (baseSelected) state.manualExcludeAssetIds.add(input.value);
      }
      refreshSelectionPreview();
    };
  });
  $("#load-more-media").disabled = !state.inspectedCursor;
  $("#load-more-media").textContent = state.inspectedCursor ? "Load more exact media" : "All loaded";
}

async function refreshSelectionPreview() {
  const version = ++state.selectionPreviewVersion;
  const hasSelection = state.selectedSessions.size || state.selectedEvents.size || state.manualIncludeAssetIds.size;
  if (!state.clusterGeneration || !hasSelection) {
    state.selectionPreview = null;
    return renderSelectionTray();
  }
  try {
    const preview = await client.previewProjectSelection(
      {clusterGenerationId: state.clusterGeneration.id},
      selectionPayload(),
    );
    if (version !== state.selectionPreviewVersion) return;
    state.selectionPreview = preview;
    renderSelectionTray();
  } catch (error) {
    if (version === state.selectionPreviewVersion) handleError(error);
  }
}

function selectionPayload() {
  return {
    sessionIds: [...state.selectedSessions],
    eventIds: [...state.selectedEvents],
    includeAssetIds: [...state.manualIncludeAssetIds],
    excludeAssetIds: [...state.manualExcludeAssetIds],
  };
}

function renderSelectionTray() {
  const preview = state.selectionPreview;
  const clusterCount = state.selectedSessions.size + state.selectedEvents.size;
  $("#selection-title").textContent = clusterCount
    ? `${countLabel(state.selectedSessions.size, "session")} · ${countLabel(state.selectedEvents.size, "event")}`
    : (state.manualIncludeAssetIds.size ? "Manual media selection" : "Nothing selected");
  $("#selection-summary").innerHTML = `<div><dt>Exact clips</dt><dd>${preview ? preview.exactAssetCount.toLocaleString() : "0"}</dd></div>
    <div><dt>Evidence span</dt><dd>${preview ? formatUs(preview.evidenceSpanUs) : "—"}</dd></div>
    <div><dt>Folders</dt><dd>${preview ? `${preview.rootCount} represented` : "—"}</dd></div>
    <div><dt>Source candidates</dt><dd>${preview ? preview.sourceCandidateCount : "—"}</dd></div>`;
  const warnings = preview
    ? [
        preview.unresolvedTimestampCount ? `${preview.unresolvedTimestampCount} unresolved timestamps` : null,
        preview.unavailableAssetCount ? `${preview.unavailableAssetCount} unavailable assets` : null,
        preview.warningAssetCount ? `${preview.warningAssetCount} clips carry timing warnings` : null,
        state.manualExcludeAssetIds.size ? `${state.manualExcludeAssetIds.size} manual exclusions` : null,
      ].filter(Boolean)
    : [];
  $("#selection-warnings").textContent = warnings.join(" · ") || (preview ? "Exact membership frozen when project is created." : "Select one or more sessions or events.");
  $("#selection-warnings").classList.toggle("warning", warnings.length > 0);
  $("#open-event").disabled = !preview?.exactAssetCount;
}

async function generateClusters() {
  if (!state.library?.catalogRevision) return toast("Complete a scan before grouping media");
  $("#generate-clusters").disabled = true;
  try {
    const job = await client.startClusterAnalysis({libraryId: state.library.id}, {
      catalogRevision: state.library.catalogRevision,
      eventGapUs: Math.round(Number($("#event-gap").value) * 1_000_000),
      sessionGapUs: Math.round(Number($("#session-gap").value) * 1_000_000),
    });
    state.clusterJob = job;
    $("#cluster-status").textContent = "Grouping coverage into sessions and events…";
    let current = job;
    while (!["SUCCEEDED", "FAILED", "CANCELED", "INTERRUPTED"].includes(current.status)) {
      await new Promise(resolve => setTimeout(resolve, 150));
      current = await client.getJob({jobId: job.id});
    }
    if (current.status !== "SUCCEEDED") throw new Error(current.message || "Clustering did not complete");
    await loadClusterGeneration();
    toast(`Created ${countLabel(current.result.sessionCount, "session")} and ${countLabel(current.result.eventCount, "event")}`);
  } finally {
    state.clusterJob = null;
    $("#generate-clusters").disabled = false;
  }
}

async function ensureMediaDetails(assetIds) {
  const missing = [...new Set(assetIds)].filter(assetId => {
    const media = state.mediaById.get(assetId);
    return !media || !Array.isArray(media.resolutions);
  });
  const results = await Promise.allSettled(missing.map(mediaId => client.getMedia({mediaId})));
  for (const result of results) {
    if (result.status === "fulfilled") state.mediaById.set(result.value.id, result.value);
  }
}

async function ensureClipMedia(clip) {
  if (!clip) return null;
  await ensureMediaDetails([clip.assetId]);
  return state.mediaById.get(clip.assetId) || null;
}

function initialProjectMediaIds(project) {
  const activeSources = project.logicalSources
    .filter(source => !source.archived)
    .sort((left, right) => Number(right.reference) - Number(left.reference))
    .slice(0, 6);
  return activeSources.flatMap(source => {
    const clip = project.clips.find(item => item.logicalSourceId === source.id);
    return clip ? [clip.assetId] : [];
  });
}

async function createProjectFromSelection() {
  if (!state.selectionPreview?.exactAssetCount) return toast("Select at least one exact media asset");
  try {
    const project = await client.createProject({}, {
      name: $("#selection-project-name").value.trim() || "Aligned security footage",
      libraryId: state.library.id,
      clusterGenerationId: state.clusterGeneration.id,
      ...selectionPayload(),
    });
    state.projects = await client.listProjects();
    renderRecentProjects();
    await openProject(project);
    toast("Evidence selected. Review timing before building the first cut.");
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
    await ensureMediaDetails(initialProjectMediaIds(project));
    state.project = project;
    await refreshPreparationState();
    state.selectedSource = 0;
    state.selectedClipId = null;
    state.selectedSegment = 0;
    invalidateReviewPreparation();
    deriveSources();
    $$('input[name="anchor"]').forEach(radio => {
      radio.checked = radio.value === (project.anchorMode === "SOURCE_TIME" ? "source-clips" : "wall-clock");
    });
    renderSources();
    renderProgram();
    renderPreparation();
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
    const firstAlignment = clipAlignment(clips[0]);
    return {
      ...source,
      clips,
      media,
      color: colors[index % colors.length],
      offsetUs: Number(firstAlignment.anchorAlignedUs || 0),
      ratePpm: Number(firstAlignment.ratePpm || 0),
    };
  });
  if (state.selectedSource >= state.sources.length) state.selectedSource = 0;
  selectedAlignmentClip();
}

function renderSources() {
  const rows = state.sources.map((source, index) => `
    <button class="source-row${index === state.selectedSource ? " active" : ""}" data-source="${index}" style="--source-color:${source.color}">
      <i></i><strong>${safe(source.label)}${source.reference ? " · REF" : ""}</strong>
      <span>${source.identityState === "PROVISIONAL" ? "Proposed identity" : "Confirmed"} · ${source.clips.length} clips</span>
    </button>`).join("");
  $("#source-list").innerHTML = rows;
  $("#cut-source-list").innerHTML = rows;
  const monitorSources = [...state.sources]
    .sort((left, right) => Number(right.id === state.sources[state.selectedSource]?.id) - Number(left.id === state.sources[state.selectedSource]?.id) || Number(right.reference) - Number(left.reference))
    .slice(0, 6);
  $("#source-monitors").innerHTML = monitorSources.map(source => {
    const index = state.sources.findIndex(item => item.id === source.id);
    return `<article class="source-monitor no-coverage${index === state.selectedSource ? " active" : ""}" data-source="${index}" data-source-id="${safe(source.id)}" tabindex="0" role="button" aria-label="Select ${safe(source.label)} source" style="--source-color:${source.color};--source-dark:${source.color}22">
      <video muted playsinline preload="auto" data-source-id="${safe(source.id)}" data-asset-id=""></video>
      <div class="monitor-placeholder" aria-hidden="true"></div>
      <div class="monitor-overlay"><span class="monitor-source">${safe(source.label)}</span><strong class="monitor-grounding">Finding exact clip…</strong><small class="monitor-time">Timeline ${formatUs(currentAlignedUs())}</small></div>
    </article>`;
  }).join("");
  renderTimelineTracks();
  const videoOptions = state.sources.map(source => `<option value="${safe(source.id)}">${safe(source.label)}</option>`).join("");
  $("#video-source").innerHTML = videoOptions;
  const fixedClipOptions = state.sources.flatMap(source => source.clips.map((clip, index) => `<option value="clip:${safe(clip.id)}">Clip ${index + 1} · ${safe(source.label)}</option>`)).join("");
  $("#audio-source").innerHTML = `<option value="follow">Follow Program Video</option><option value="silence">Intentional silence</option>${state.sources.map(source => `<option value="source:${safe(source.id)}">Fixed source · ${safe(source.label)}</option>`).join("")}${fixedClipOptions}`;
  $$('[data-source]').forEach(button => {
    button.onclick = async () => {
      const nextSource = Number(button.dataset.source);
      if (nextSource !== state.selectedSource) state.selectedClipId = null;
      state.selectedSource = nextSource;
      renderSources();
      await ensureClipMedia(selectedAlignmentClip());
      renderSourceInspector();
      renderProgram();
    };
  });
  $$("#source-monitors [data-source]").forEach(monitor => {
    monitor.onkeydown = event => {
      if (["Enter", " "].includes(event.key)) {
        event.preventDefault();
        monitor.click();
      }
    };
  });
  bindAlignmentTrackInteractions();
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
  $("#manage-clip").onchange = async event => {
    state.selectedClipId = event.target.value || null;
    await ensureClipMedia(selectedAlignmentClip(source));
    renderSourceInspector();
  };
}

function trackMarkup(source) {
  const spanUs = Math.max(1, state.timelineEndUs - state.timelineStartUs);
  const items = (state.timelineWindow?.items || []).filter(item =>
    item.type === "CLIP"
      ? item.logicalSourceId === source.id
      : item.logicalSourceIds?.includes(source.id)
  );
  const clips = items.map(item => {
    const startUs = Number(item.startAlignedUs);
    const endUs = Number(item.endAlignedUs);
    const left = Math.max(0, Math.min(100, ((startUs - state.timelineStartUs) / spanUs) * 100));
    const width = Math.max(0.18, Math.min(100 - left, ((endUs - startUs) / spanUs) * 100));
    if (item.type === "BUCKET") {
      return `<i class="clip aggregate" title="${item.clipCount} clips in this evidence bucket" style="left:${left}%;width:${width}%;--source-color:${source.color}"></i>`;
    }
    const provisional = item.alignmentState === "PROVISIONAL" ? " provisional" : "";
    const warning = item.warnings?.length ? " warning" : "";
    return `<button class="clip${provisional}${warning}" data-timeline-clip="${safe(item.clipId)}" aria-label="${safe(item.relativePath)} · ${safe(item.alignmentState)}" title="${safe(item.relativePath)} · ${safe(item.alignmentState)}" style="left:${left}%;width:${width}%;--source-color:${source.color}"></button>`;
  }).join("");
  return `<div class="track-row" data-drag-source="${safe(source.id)}" tabindex="0" aria-label="${safe(source.label)} evidence track with ${source.clips.length} clips; use arrow keys for 10 millisecond nudges"><div class="track-label"><strong>${safe(source.label)}</strong><small>${source.clips.length} clips · ${source.identityState === "PROVISIONAL" ? "identity to review" : "confirmed source"}</small></div><div class="track-clips">${clips}</div></div>`;
}

function renderSourceInspector() {
  const source = state.sources[state.selectedSource];
  if (!source) return;
  const clip = selectedAlignmentClip(source);
  const alignment = clipAlignment(clip);
  const media = clip ? state.mediaById.get(clip.assetId) : null;
  $("#selected-source-name").textContent = source.label;
  $("#source-label").value = source.label;
  $("#sync-offset").value = Math.round(Number(alignment.anchorAlignedUs || 0) / 1000);
  $("#sync-rate").value = Number(alignment.ratePpm || 0);
  $("#confidence-label").textContent = clip?.alignmentState === "ACCEPTED"
    ? "Timing approved for the first cut"
    : clip?.alignmentState === "PROVISIONAL"
      ? "Timestamp estimate needs review"
      : "Timing needs a manual decision";
  $("#provenance-panel").innerHTML = provenanceMarkup(media) + (media ? `<form id="provenance-correction"><p class="eyebrow">Revisioned correction</p><label class="field"><span>Field</span><input id="resolution-field" required maxlength="100" placeholder="capturedAt"></label><label class="field"><span>Resolved value</span><input id="resolution-value" required maxlength="500"></label><label class="field"><span>Rationale (optional)</span><input id="resolution-rationale" maxlength="500"></label><button class="btn wide" type="submit">Record correction</button></form>` : "");
  if ($("#provenance-correction")) $("#provenance-correction").onsubmit = event => recordProvenanceCorrection(event, media);
}

function renderPreparation() {
  const preparation = state.preparation;
  if (!preparation) return;
  const source = state.sources[state.selectedSource];
  const clip = selectedAlignmentClip(source);
  const alignment = preparation.alignment;
  const evidenceDuration = alignment.evidenceSpan.durationUs;
  $("#event-title").textContent = state.project.name;
  $("#preparation-summary").innerHTML = `
    <div><small>Preparation phase</small><strong>${safe(preparation.phase.replaceAll("_", " "))}</strong></div>
    <div><small>Evidence span</small><strong>${formatUs(evidenceDuration)}</strong></div>
    <div><small>Proposed output</small><strong>${formatUs(alignment.proposedOutputDurationUs)}</strong></div>
    <div class="${preparation.blockers.length ? "blocked" : ""}"><small>Readiness</small><strong>${preparation.blockers.length ? `${preparation.blockers.length} action${preparation.blockers.length === 1 ? "" : "s"} required` : "Ready to compose"}</strong></div>`;
  const confirmButton = $("#confirm-source-identities");
  confirmButton.hidden = preparation.sourceIdentity.provisionalCount === 0;
  confirmButton.textContent = preparation.sourceIdentity.provisionalCount
    ? `Confirm ${preparation.sourceIdentity.provisionalCount} proposed source ${preparation.sourceIdentity.provisionalCount === 1 ? "identity" : "identities"}`
    : "Source identities confirmed";
  $("#analyze-alignment").disabled = !preparation.canAnalyzeAlignment || !preparation.sourceIdentity.ready;
  $("#build-first-cut").disabled = !preparation.canGenerateProgramDraft;
  $("#composition-actions").classList.toggle("pending", preparation.blockers.length > 0 && !preparation.canGenerateProgramDraft);
  $("#build-first-cut").textContent = preparation.legacyProgramTruncation
    ? "Repair truncated program"
    : preparation.hasProgram ? "Rebuild first cut" : "Build first cut";
  $("#continue-cut").disabled = !preparation.canEnterCut;
  const unplaced = state.timelineWindow?.unplacedCount || 0;
  $("#confidence-label").closest(".confidence").querySelector("p").textContent = unplaced
    ? `${unplaced} clip${unplaced === 1 ? " has" : "s have"} no usable time yet. Use the workbench to locate and place ${unplaced === 1 ? "it" : "them"}.`
    : clip?.alignmentState === "ACCEPTED"
      ? "This clip can be used automatically. Its exact timing and source remain recorded in provenance."
      : "This clip remains visible, but it will not be used automatically until its timing is approved.";
  $("#alignment-task-copy").textContent = preparation.blockers.length
    ? `${preparation.blockers.length} required timeline ${preparation.blockers.length === 1 ? "decision blocks" : "decisions block"} the first cut. Follow the recommended action; optional warnings can wait.`
    : preparation.canGenerateProgramDraft
      ? "Required timing decisions are complete. Build the first cut now; any remaining warnings are optional review."
      : "Analyze overlaps to compare clip audio. Then approve timestamp estimates only where audio cannot give a stronger answer.";
  syncWorkflowAvailability();
}

/**
 * Renders the alignment suggestion queues, evidence summary, blockers, and review actions.
 */
function renderSuggestions() {
  const container = $("#suggestion-list");
  const proposalSet = state.proposalSets.find(item =>
    ["PENDING", "PARTIALLY_RESOLVED", "PARTIALLY_ACCEPTED"].includes(item.status)
    && item.projectRevision === state.project.revision
  ) || state.proposalSets[0];
  if (!proposalSet) {
    $("#analyze-alignment").textContent = "Analyze clip overlaps";
    $("#analyze-alignment").classList.add("primary");
    const unplaced = state.timelineWindow?.unplacedItems || [];
    container.innerHTML = unplaced.length
      ? `<div class="review-queue">${unplaced.slice(0, 12).map(item => `<div class="confidence"><strong>Unplaced clip</strong><p><a class="timeline-clip-link" href="#alignment-timeline" data-focus-clip="${safe(item.clipId)}">${safe(item.relativePath || item.assetId)}</a></p><small>Manual timing evidence required</small></div>`).join("")}</div>`
      : `<p class="muted"><strong>No overlap analysis yet.</strong><br>Start the analysis to check whether sound shared between cameras can improve their timestamp estimates. Nothing is changed until you approve a result.</p>`;
    return;
  }
  const summary = proposalSet.summary;
  const accepted = new Set(proposalSet.acceptedProposalIds || []);
  const rejected = new Set(proposalSet.rejectedProposalIds || []);
  const highConfidence = proposalSet.proposals.filter(item => item.automaticallyAcceptable && !accepted.has(item.id) && !rejected.has(item.id));
  const pending = proposalSet.proposals.filter(item => !accepted.has(item.id) && !rejected.has(item.id));
  const review = pending.filter(item => !item.automaticallyAcceptable);
  const timestampOnly = review.filter(item => {
    const clip = clipById(item.clipId);
    const clipAlreadyAccepted = clip && (clip.alignmentState || (clip.sync ? "ACCEPTED" : "UNRESOLVED")) === "ACCEPTED";
    const backendAllowsTimestampAcceptance = item.timestampPriorAcceptable ?? item.classification === "TIMESTAMP_ONLY";
    return backendAllowsTimestampAcceptance && !clipAlreadyAccepted;
  });
  const blockers = state.alignmentSummary?.blockers || [];
  const warnings = state.alignmentSummary?.warnings || [];
  const blockerClipIds = new Set(blockers.flatMap(item => item.clipIds || []));
  const compositionBlockers = blockers.filter(item => item.code === "TIMELINE_SECTION_REQUIRED");
  const blockerReview = review.filter(item => blockerClipIds.has(item.clipId));
  const orderedReview = [...blockerReview, ...review.filter(item => !blockerClipIds.has(item.clipId))];
  const visibleReview = orderedReview.slice(0, state.alignmentQueueLimit);
  const nextRequired = blockerReview[0] || (blockers.length ? review[0] : null);
  const canBuild = blockers.length === 0 && state.preparation?.canGenerateProgramDraft;
  const canContinue = blockers.length === 0 && state.preparation?.canEnterCut;
  const recommendedAction = highConfidence.length
    ? `<button class="btn primary recommended-action" id="accept-high-confidence">Approve all ${highConfidence.length} safe audio ${highConfidence.length === 1 ? "match" : "matches"}<small>Recommended · applies the strongest evidence first</small></button>`
    : blockers.length && timestampOnly.some(item => blockerClipIds.has(item.clipId))
      ? `<button class="btn primary recommended-action" id="review-timestamp-priors">Review timestamp estimates needed for coverage<small>Preview their effect before anything is approved</small></button>`
      : compositionBlockers.length && state.preparation?.canGenerateProgramDraft
        ? `<button class="btn primary recommended-action" data-build-recommended="true">Resolve uncovered time and rebuild the first cut<small>Recommended · uses your “Time with no recorded footage” choice below</small></button>`
        : nextRequired
        ? `<button class="btn primary recommended-action" data-focus-clip="${safe(nextRequired.clipId)}" data-open-review-queue="true">Review the next required clip<small>We’ll locate it and show the decisions you can make</small></button>`
        : canContinue
          ? `<button class="btn primary recommended-action" data-continue-recommended="true">Continue to Cut<small>Timing and the first cut are ready</small></button>`
          : canBuild
          ? `<button class="btn primary recommended-action" data-build-recommended="true">Build the first cut<small>Required timing decisions are complete</small></button>`
          : `<p class="help">No automatic next action is available. Select an unresolved clip from the review queue to place it manually.</p>`;
  const currentStep = highConfidence.length ? 2 : blockers.length ? 3 : 4;
  const analyzeButton = $("#analyze-alignment");
  analyzeButton.textContent = "Run overlap analysis again";
  analyzeButton.classList.remove("primary");
  container.innerHTML = `
    <ol class="alignment-steps" aria-label="Alignment workflow">
      <li class="complete"><span>1</span><div><strong>Analyze overlaps</strong><small>Complete</small></div></li>
      <li class="${currentStep === 2 ? "current" : highConfidence.length ? "upcoming" : "complete"}"><span>2</span><div><strong>Approve safe matches</strong><small>${highConfidence.length ? `${highConfidence.length} waiting` : "Complete"}</small></div></li>
      <li class="${currentStep === 3 ? "current" : blockers.length ? "upcoming" : "complete"}"><span>3</span><div><strong>Resolve required time</strong><small>${blockers.length ? `${blockers.length} blocking` : "Complete"}</small></div></li>
      <li class="${currentStep === 4 ? "current" : "upcoming"}"><span>4</span><div><strong>${state.preparation?.hasProgram ? "Continue to Cut" : "Build first cut"}</strong><small>${currentStep === 4 ? "Ready" : "After required time"}</small></div></li>
    </ol>
    <div class="recommended-next"><p class="eyebrow">Recommended next action</p>${recommendedAction}</div>
    <div class="alignment-overview" role="list" aria-label="Alignment status"><div role="listitem"><strong>${highConfidence.length}</strong><small>safe audio matches waiting</small></div><div role="listitem"><strong>${timestampOnly.length}</strong><small>timestamp estimates available</small></div><div role="listitem"><strong>${blockers.length}</strong><small>required timeline decisions</small></div></div>
    ${blockers.length ? `<div class="confidence blocked"><strong>Required before the first cut</strong><p>${blockers.map(item => item.code === "TIMELINE_SECTION_REQUIRED" ? `${formatUs(Number(item.endAlignedUs || 0) - Number(item.startAlignedUs || 0))} has no recorded footage. Choose how to handle it below.` : `${formatUs(Number(item.endAlignedUs || 0) - Number(item.startAlignedUs || 0))} needs an approved clip`).join("<br>")}</p></div>` : ""}
    ${review.length ? `<details class="alignment-review-cases" ${blockerReview.length ? "open" : ""}><summary>Optional clip review queue <span>${review.length} clips · ${blockerReview.length} required</span></summary><p class="help">Open this only when the recommended action asks you to review a clip, or when you want to inspect optional evidence. Optional clips do not prevent the first cut.</p><div class="review-queue">${visibleReview.map(item => `<div class="confidence ${blockerClipIds.has(item.clipId) ? "required-review" : ""}"><strong>${blockerClipIds.has(item.clipId) ? "Required · " : "Optional · "}${item.classification === "TIMESTAMP_ONLY" ? "Timestamp estimate" : item.classification === "CONFLICTING" ? "Evidence disagrees" : "Timing not found"}</strong><p><a class="timeline-clip-link" href="#alignment-timeline" data-focus-clip="${safe(item.clipId)}">${safe(state.mediaById.get(item.assetId)?.relative_path || item.assetId)}</a></p><small>${safe((item.limitations || []).join("; ") || "Open this clip on the timeline before deciding")}</small><div class="link-row"><button class="btn primary" data-accept-proposal="${safe(item.id)}">Approve this timing</button><button class="btn" data-hold-clip="${safe(item.clipId)}">Review later</button><button class="btn" data-exclude-clip="${safe(item.clipId)}">Do not use automatically</button><button class="btn" data-reject-proposal="${safe(item.id)}">Reject this result</button></div></div>`).join("")}</div>${orderedReview.length > visibleReview.length ? `<button class="btn wide" id="load-more-alignment">Show ${Math.min(50, orderedReview.length - visibleReview.length)} more</button>` : ""}</details>` : ""}
    <details class="alignment-details"><summary>Technical analysis details</summary><div class="proposal-grid"><div><strong>${summary.audioConfirmed}</strong><small>audio-supported clips</small></div><div><strong>${summary.timestampOnly}</strong><small>timestamp-only clips</small></div><div><strong>${summary.conflicting}</strong><small>conflicts</small></div><div><strong>${summary.unresolved}</strong><small>unresolved</small></div></div><small>${summary.candidatePairs} bounded comparisons · ${summary.confirmedEdges} supported audio links · ±${Math.round(Number(proposalSet.config?.overlapSearchExtensionUs || state.settings.overlapSearchExtensionUs) / 1_000_000)}s search window · ${warnings.length} non-blocking warnings</small></details>`;
  if ($("#accept-high-confidence")) $("#accept-high-confidence").onclick = () => acceptHighConfidence(proposalSet);
  if ($("#review-timestamp-priors")) $("#review-timestamp-priors").onclick = () => acceptTimestampPriors(proposalSet);
  if ($('[data-build-recommended]')) $('[data-build-recommended]').onclick = () => $("#build-first-cut").click();
  if ($('[data-continue-recommended]')) $('[data-continue-recommended]').onclick = () => $("#continue-cut").click();
  if ($("#load-more-alignment")) $("#load-more-alignment").onclick = () => { state.alignmentQueueLimit += 50; renderSuggestions(); };
  $$('[data-accept-proposal]').forEach(button => { button.onclick = () => resolveAlignmentProposal(proposalSet, button.dataset.acceptProposal, true); });
  $$('[data-reject-proposal]').forEach(button => { button.onclick = () => resolveAlignmentProposal(proposalSet, button.dataset.rejectProposal, false); });
  $$('[data-hold-clip]').forEach(button => { button.onclick = () => setClipEligibility(button.dataset.holdClip, "HELD_FOR_REVIEW"); });
  $$('[data-exclude-clip]').forEach(button => { button.onclick = () => setClipEligibility(button.dataset.excludeClip, "EXCLUDED"); });
}

/**
 * Accepts non-conflicting alignment proposals using timestamp-prior placement within a selected scope.
 * @param {Object} proposalSet - The proposal set containing the timestamp-prior alignments to accept.
 */
async function acceptTimestampPriors(proposalSet) {
  try {
    const scope = await chooseTimestampScope();
    if (!scope) return;
    const preview = await client.createAlignmentAcceptancePreview({projectId: state.project.id}, {
      commandId: crypto.randomUUID(), expectedRevision: state.project.revision,
      proposalSetId: proposalSet.id, proposalSetDigest: proposalSet.digest,
      mode: "TIMESTAMP_PRIOR", scope,
    });
    const remainingBlockers = Number(preview.remainingBlockerCount ?? 0);
    const message = `${preview.affectedClipCount} non-conflicting clips will use timestamp-prior placement.\n\nAccepted coverage: ${formatUs(preview.acceptedCoverageBeforeUs)} → ${formatUs(preview.acceptedCoverageAfterUs)}\nRemaining blockers: ${preview.resultingReadiness ? "none" : remainingBlockers}\n\nWireless-camera timestamps may reflect hub receipt time rather than exact capture time. Conflicting clips are excluded from this action.`;
    if (!await confirmAction(message, {title: "Accept timestamp placements", confirmLabel: "Accept placements"})) return;
    if (await command("AcceptAlignmentProposalSet", {
      proposalSetId: proposalSet.id, digest: proposalSet.digest, mode: "TIMESTAMP_PRIOR", scope,
      previewId: preview.id, previewDigest: preview.digest, confirmTimestampUncertainty: true,
    })) toast(`${preview.affectedClipCount} timestamp placements accepted as one revision`);
  } catch (error) { handleError(error); }
}

/**
 * Prompts the user to choose the scope for reviewing timestamp-prior alignment proposals.
 * @return {Promise<Object|null>} The selected scope, or `null` if the dialog is cancelled.
 */
function chooseTimestampScope() {
  const dialog = $("#timestamp-scope-dialog");
  const select = $("#timestamp-scope-select");
  const source = state.sources[state.selectedSource];
  const clip = selectedAlignmentClip(source);
  const options = [
    {label: "Entire project", scope: {kind: "PROJECT"}},
    ...(source ? [{label: `Current source · ${source.label}`, scope: {kind: "SOURCES", sourceIds: [source.id]}}] : []),
    ...(clip ? [{label: "Selected clip", scope: {kind: "CLIPS", clipIds: [clip.id]}}] : []),
    {label: `Visible timeline · ${formatUs(state.timelineEndUs - state.timelineStartUs)}`, scope: {kind: "ALIGNED_RANGE", startAlignedUs: Math.round(state.timelineStartUs), endAlignedUs: Math.round(state.timelineEndUs)}},
    ...(state.selectedEvents.size ? [{label: `${state.selectedEvents.size} selected event${state.selectedEvents.size === 1 ? "" : "s"}`, scope: {kind: "EVENTS", eventIds: [...state.selectedEvents]}}] : []),
  ];
  select.innerHTML = options.map((item, index) => `<option value="${index}">${safe(item.label)}</option>`).join("");
  return new Promise(resolve => {
    const finish = () => resolve(dialog.returnValue === "preview" ? options[Number(select.value)]?.scope || null : null);
    dialog.addEventListener("close", finish, {once: true});
    dialog.showModal();
  });
}

/**
 * Sets whether a clip is eligible for inclusion in the program.
 * @param {string} clipId - The identifier of the clip.
 * @param {boolean} programEligibility - Whether the clip is eligible for the program.
 */
async function setClipEligibility(clipId, programEligibility) {
  await command("SetClipProgramEligibility", {clipIds: [clipId], programEligibility});
}

/**
 * Focuses a project clip in the alignment timeline and adjusts the view to its aligned interval.
 * @param {string} clipId - The identifier of the clip to focus.
 * @param {Object|null} [proposalSet=null] - Optional proposal set containing alignment data for the clip.
 */
async function focusClipInTimeline(clipId, proposalSet = null) {
  const clip = clipById(clipId);
  if (!clip) return toast("This clip is no longer part of the project");
  await ensureClipMedia(clip);
  const proposal = proposalSet?.proposals?.find(item => item.clipId === clipId);
  const alignment = proposal?.proposedAlignment || clipAlignment(clip);
  const media = state.mediaById.get(clip.assetId);
  const rate = (1_000_000 + Number(alignment.ratePpm || 0)) / 1_000_000;
  const startUs = Number(alignment.anchorAlignedUs || 0) - Number(alignment.anchorSourceUs || 0) * rate;
  const durationUs = Math.max(1, Number(media?.durationUs || 0) * rate);
  const centerUs = startUs + durationUs / 2;
  const fullSpanUs = Math.max(1, state.evidenceEndUs - state.evidenceStartUs);
  const targetSpanUs = Math.min(fullSpanUs, Math.max(10_000_000, durationUs * 4));
  const targetZoom = fullSpanUs <= targetSpanUs ? 0 : Math.min(100, Math.max(0, 20 * Math.log2(fullSpanUs / targetSpanUs)));
  state.selectedClipId = clipId;
  const sourceIndex = state.sources.findIndex(source => source.id === clip.logicalSourceId);
  if (sourceIndex >= 0) state.selectedSource = sourceIndex;
  setPlayhead(((centerUs - state.evidenceStartUs) / fullSpanUs) * 100);
  await changeTimelineZoom(targetZoom, centerUs);
  renderSourceInspector();
  requestAnimationFrame(() => {
    const target = $(`#alignment-tracks [data-timeline-clip="${CSS.escape(clipId)}"]`);
    target?.classList.add("focused");
    target?.focus({preventScroll: true});
    $("#alignment-timeline").scrollIntoView({behavior: "smooth", block: "center"});
  });
}

async function acceptHighConfidence(proposalSet) {
  const count = proposalSet.proposals.filter(item => item.automaticallyAcceptable).length;
  if (!await confirmAction(`Accept ${count} audio-confirmed placements as one reversible revision? Timestamp-only and conflicting clips will remain for review.`, {title: "Accept alignment results", confirmLabel: "Accept results"})) return;
  if (await command("AcceptAlignmentProposalSet", {
    proposalSetId: proposalSet.id,
    digest: proposalSet.digest,
    mode: "HIGH_CONFIDENCE",
    confirmDrift: proposalSet.proposals.some(item => item.automaticallyAcceptable && item.requiresDriftConfirmation),
  })) toast("High-confidence alignment accepted; remaining clips are still held for review");
}

async function resolveAlignmentProposal(proposalSet, proposalId, accept) {
  const proposal = proposalSet.proposals.find(item => item.id === proposalId);
  if (!proposal) return;
  if (accept && !await confirmAction(`Accept this ${proposal.classification.toLocaleLowerCase().replaceAll("_", " ")} placement as an explicit manual decision?`, {title: "Accept clip placement", confirmLabel: "Accept placement"})) return;
  await command(accept ? "AcceptAlignmentProposal" : "RejectAlignmentProposal", {
    proposalSetId: proposalSet.id,
    proposalId,
    digest: proposalSet.digest,
    ...(accept ? {confirmLowConfidence: true, confirmDrift: proposal.requiresDriftConfirmation} : {}),
  });
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
    await refreshPreparationState();
    invalidateReviewPreparation();
    deriveSources();
    renderSources();
    renderProgram();
    renderPreparation();
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
  state.programDurationUs = Math.max(1, Number(state.compiled.durationUs || 0));
  state.durationUs = activeClockDurationUs();
  const videoMarkup = video.map((block, index) => segmentMarkup(block, index, state.programDurationUs)).join("");
  const audioMarkup = audio.map((block, index) => segmentMarkup(block, index, state.programDurationUs, true)).join("");
  const videoLaneMarkup = videoMarkup || '<div class="program-empty">No Program Video yet · align evidence before building the first cut</div>';
  const audioLaneMarkup = audioMarkup || '<div class="program-empty">No Program Audio yet · audio decisions begin with the first cut</div>';
  $("#video-lane").innerHTML = videoLaneMarkup;
  $("#audio-lane").innerHTML = audioLaneMarkup;
  $("#align-video-lane").innerHTML = videoMarkup
    ? videoMarkup.replaceAll("data-segment", "data-align-segment")
    : '<div class="program-empty">Program generation waits for accepted alignment</div>';
  $("#align-audio-lane").innerHTML = audioMarkup
    ? audioMarkup.replaceAll("data-audio", "data-align-audio")
    : '<div class="program-empty">Independent audio decisions will appear here</div>';
  $$('[data-segment], [data-align-segment]').forEach(node => {
    node.onclick = () => {
      state.selectedSegment = Number(node.dataset.segment ?? node.dataset.alignSegment);
      const block = currentVideoBlock();
      if (block) setPlayhead((block.startUs / state.programDurationUs) * 100 + 0.01);
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
  $("#review-duration").textContent = formatUs(state.compiled.durationUs || 0);
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
    const operation = preview ? client.applyProjectCommand.bind(client) : client.applyProjectDeltaCommand.bind(client);
    const result = await operation({
      projectId: state.project.id,
      query: preview ? {preview: "true"} : {},
    }, {
      commandId: crypto.randomUUID(),
      expectedRevision: state.project.revision,
      commandType,
      payload,
    });
    if (preview) return result;
    const delta = result.changedEntities || {};
    state.project = {...state.project, ...(delta.set || {})};
    for (const key of delta.remove || []) delete state.project[key];
    state.project.revision = result.appliedRevision;
    await refreshPreparationState();
    invalidateReviewPreparation();
    deriveSources();
    renderSources();
    renderProgram();
    renderPreparation();
    $("#footer-status").textContent = `${state.project.name} · revision ${state.project.revision} · backend-authoritative ${state.view}`;
    return {...result, project: state.project};
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
  if (!await confirmAction(`Apply this scoped reconciliation? ${remaining.length} video issue(s) would remain.`, {title: "Reconcile boundary", confirmLabel: "Apply reconciliation"})) return;
  await command("ReconcileBoundary", payload);
  toast("Selected boundary reconciled explicitly");
}

async function buildFirstCut() {
  if (!state.project || !state.preparation?.canGenerateProgramDraft) return;
  const gapMode = $("#gap-policy").value;
  try {
    const proposal = await client.getTimelineSectionProposal({
      projectId: state.project.id,
      query: {gapMode},
    });
    const replaceExisting = Boolean(state.project.videoBlocks?.length);
    const payload = {
      alignmentDigest: proposal.alignmentDigest,
      selectionDigest: state.project.selectionSnapshot.digest,
      gapMode,
      sectionProposalDigest: proposal.digest,
      replaceExisting,
    };
    const preview = await command("GenerateProgramDraft", payload, {preview: true});
    if (!preview) return;
    const draft = preview.project.programDraft;
    const gapDecision = gapMode === "SLATE"
      ? `${formatUs(proposal.slateDurationUs)} represented by generated slates and deliberate silence`
      : `${formatUs(proposal.excludedDurationUs)} excluded from the output clock`;
    const replacement = replaceExisting
      ? `\n\nThis creates a new revision and replaces the existing ${formatUs(state.preparation.programDurationUs)} Program. Existing source evidence and prior revisions remain unchanged.`
      : "";
    const message = [
      `Build this first cut from the accepted alignment?`,
      `Output duration: ${formatUs(proposal.outputDurationUs)}`,
      `Covered evidence: ${formatUs(proposal.keepDurationUs)}`,
      `Downtime: ${gapDecision}`,
      `Optimized source changes: ${draft.sourceChanges}`,
    ].join("\n");
    if (!await confirmAction(message + replacement, {title: replaceExisting ? "Rebuild first cut" : "Build first cut", confirmLabel: replaceExisting ? "Rebuild program" : "Build program"})) return;
    const result = await command("GenerateProgramDraft", payload);
    if (!result) return;
    state.selectedSegment = 0;
    toast(replaceExisting
      ? "Program rebuilt as a new revision from accepted evidence"
      : "First cut built from accepted evidence and explicit gap decisions");
    showView("cut");
  } catch (error) {
    handleError(error);
  }
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
  state.durationUs = activeClockDurationUs();
  updatePlayheadPositions();
  $(".scrubber").value = state.playhead;
  $("#align-time").textContent = formatUs(currentAlignedUs());
  $("#align-clock").textContent = `EVIDENCE ${formatUs(currentAlignedUs())}`;
  $("#program-clock").textContent = `OUTPUT ${formatUs(currentOutputUs())}`;
  const index = sortedVideoBlocks().findIndex(block => block.startUs <= currentOutputUs() && currentOutputUs() < block.endUs);
  if (index >= 0) state.selectedSegment = index;
  renderProgram();
  syncSourceMonitors();
  clearTimeout(setPlayhead.pointTimer);
  setPlayhead.pointTimer = setTimeout(async () => {
    if (!state.preparation?.canEnterCut) return;
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

function updatePlayheadPositions() {
  $$(".playhead:not(.alignment-playhead)").forEach(node => { node.style.left = `${state.playhead}%`; });
  const alignedUs = currentAlignedUs();
  const visibleSpanUs = Math.max(1, state.timelineEndUs - state.timelineStartUs);
  const position = ((alignedUs - state.timelineStartUs) / visibleSpanUs) * 100;
  const alignmentPlayhead = $(".alignment-playhead");
  if (alignmentPlayhead) {
    alignmentPlayhead.style.left = `${Math.max(0, Math.min(100, position))}%`;
    alignmentPlayhead.hidden = position < 0 || position > 100;
  }
}

function togglePlay() {
  state.playing = !state.playing;
  $$('[data-action="play"]').forEach(button => { button.textContent = state.playing ? "Ⅱ" : "▶"; });
  clearInterval(state.timer);
  syncSourceMonitors(state.playing);
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
    videoCodec: state.settings.renderVideoCodec,
    resolution: state.settings.renderResolution,
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
    const suffix = outputSuffixFor(request.profile, request.videoCodec);
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
      {outputGrantId: grant.id, filename, profile: request.profile, videoCodec: request.videoCodec, resolution: request.resolution},
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

function outputSuffixFor(profile, videoCodec) {
  if (profile === "ARCHIVAL_LOSSLESS") return ".mkv";
  return videoCodec === "PRORES_VIDEOTOOLBOX" ? ".mov" : ".mp4";
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
    ["Reusable program", (plan.programDigest || plan.planDigest).slice(0, 16) + "…"],
    ["Default output", `${safe(plan.renderVideoCodec)} · ${safe(plan.renderResolution)}`],
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
    const rawPath = $("#output-path").value.trim();
    const separator = rawPath.lastIndexOf("/");
    if (separator <= 0 || separator === rawPath.length - 1) throw new Error("Output must be an absolute file path");
    const directory = rawPath.slice(0, separator) || "/";
    const filename = rawPath.slice(separator + 1);
    const expectedSuffix = outputSuffixFor(plan.profile, state.settings.renderVideoCodec);
    if (!filename.toLowerCase().endsWith(expectedSuffix)) {
      throw new Error(`${state.settings.renderVideoCodec} output filename must end with ${expectedSuffix}`);
    }
    let grant = state.outputGrantByDirectory.get(directory);
    if (!grant || grant.revoked) {
      grant = await client.createGrant({}, {path: directory, role: "WRITE_OUTPUT"});
      state.outputGrantByDirectory.set(directory, grant);
    }
    const result = await client.startRender({planId: plan.id}, {
      outputGrantId: grant.id,
      filename,
      videoCodec: state.settings.renderVideoCodec,
      resolution: state.settings.renderResolution,
    });
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

function selectedScanRequest(rootIds) {
  const mode = $("#scan-mode").value;
  const request = {mode, rootIds};
  if (mode === "BOUNDED") request.limit = Math.max(1, normalizeInteger($("#scan-limit").value, 250));
  return request;
}

async function scanRoots(rootIds) {
  if (!state.library) throw new Error("Create a library and add a folder first");
  state.scanJob = await client.startScan(
    {libraryId: state.library.id},
    selectedScanRequest(rootIds),
  );
  state.scanDetail = state.scanJob;
  renderLibraryRoots();
  const panel = $("#scan-progress");
  panel.classList.remove("hidden");
  let scan;
  const detailTimer = setInterval(() => {
    client.getScan({scanId: state.scanJob?.id}).then(detail => {
      state.scanDetail = detail;
      renderLibraryRoots();
    }).catch(() => {});
  }, 750);
  try {
    scan = await pollJob(state.scanJob.id, job => {
      $("#scan-count").textContent = `${Math.round((job.progress || 0) * 100)}% · ${job.message || "Scanning"}`;
    });
  } finally {
    clearInterval(detailTimer);
    if (state.scanJob?.id) {
      try { state.scanDetail = await client.getScan({scanId: state.scanJob.id}); } catch (_error) {}
    }
    state.scanJob = null;
    panel.classList.add("hidden");
    renderLibraryRoots();
  }
  if (scan.status !== "SUCCEEDED") throw new Error(scan.message || `Scan ${scan.status.toLowerCase()}`);
  await refreshCurrentLibrary();
  await loadMediaPage(true);
  await loadClusterGeneration();
  toast("Read-only scan complete; warnings and incomplete evidence remain inspectable");
}

async function addFolderAndScan(path) {
  if (!path) throw new Error("Choose a folder path to add");
  if (!state.library) {
    state.library = await client.createLibrary({}, {
      name: $("#library-name").value.trim() || "Video library",
      timeZone: $("#library-time-zone").value || "UTC",
      dstFold: Number($("#library-dst-fold").value),
      nonexistentPolicy: $("#library-nonexistent").value,
    });
  }
  const grant = await client.createGrant({}, {path, role: "READ_ONLY_SOURCE"});
  const root = await client.addLibraryRoot(
    {libraryId: state.library.id},
    {grantId: grant.id},
  );
  await refreshCurrentLibrary();
  $("#library-path").value = "";
  await scanRoots([root.id]);
}

/**
 * Registers event handlers for navigation, settings, media scanning, alignment, editing, playback, timeline controls, review, rendering, downloads, and keyboard shortcuts.
 */
function setupEvents() {
  $$('[data-view]').forEach(button => { button.onclick = () => showView(button.dataset.view); });
  $("#open-settings").onclick = () => {
    populateSettingsForm();
    $("#settings-save-state").textContent = "";
    $("#settings-dialog").showModal();
  };
  $("#close-settings").onclick = () => closeSettings();
  $("#cancel-settings").onclick = () => closeSettings();
  $("#settings-dialog").addEventListener("cancel", event => {
    event.preventDefault();
    closeSettings();
  });
  $("#settings-form").onsubmit = saveSettings;
  $("#text-scale").onchange = previewSettingsForm;
  $("#color-scheme").onchange = previewSettingsForm;
  $("#suggestion-list").addEventListener("click", event => {
    const link = event.composedPath().find(node => node instanceof Element && node.matches?.("[data-focus-clip]"));
    if (!link) return;
    event.preventDefault();
    if (link.dataset.openReviewQueue) {
      const queue = $(".alignment-review-cases");
      if (queue) queue.open = true;
    }
    toast("Locating clip in the evidence timeline…");
    const proposalSet = state.proposalSets.find(item => item.proposals?.some(proposal => proposal.clipId === link.dataset.focusClip));
    focusClipInTimeline(link.dataset.focusClip, proposalSet).catch(handleError);
  });
  $("#scan-form").onsubmit = async event => {
    event.preventDefault();
    try {
      await addFolderAndScan($("#library-path").value.trim());
    } catch (error) {
      state.scanJob = null;
      $("#scan-progress").classList.add("hidden");
      renderLibraryRoots();
      handleError(error);
    }
  };
  $("#scan-mode").onchange = event => {
    $("#scan-limit-field").classList.toggle("hidden", event.target.value !== "BOUNDED");
  };
  $("#scan-all-folders").onclick = () => scanRoots(
    (state.library?.roots || []).filter(root => root.active).map(root => root.id),
  ).catch(error => {
    state.scanJob = null;
    $("#scan-progress").classList.add("hidden");
    renderLibraryRoots();
    handleError(error);
  });
  $("#generate-clusters").onclick = () => generateClusters().catch(handleError);
  $("#load-more-sessions").onclick = () => loadSessions(false).catch(handleError);
  $("#load-more-media").onclick = () => {
    const cluster = state.inspectedCluster;
    if (cluster) inspectCluster(cluster.kind, cluster.id, cluster.parentSessionId, true).catch(handleError);
  };
  $("#show-unclustered").onclick = () => inspectCluster(
    "UNCLUSTERED", state.clusterGeneration?.id || "", null, false,
  ).catch(handleError);
  for (const selector of ["#session-date-filter", "#session-root-filter", "#session-source-filter", "#session-warning-filter"]) {
    $(selector).onchange = () => loadSessions(true).catch(handleError);
  }
  $("#apply-time-policy").onclick = async () => {
    if (!state.library) return toast("Index or open a library first");
    try {
      state.library = await client.updateLibraryTimePolicy({libraryId: state.library.id}, {timeZone: $("#library-time-zone").value, dstFold: Number($("#library-dst-fold").value), nonexistentPolicy: $("#library-nonexistent").value});
      await loadMediaPage(true);
      await loadClusterGeneration();
      toast("Timestamp policy applied; raw evidence retained and suggestions invalidated");
    } catch (error) { handleError(error); }
  };
  $("#open-event").onclick = createProjectFromSelection;
  $("#sync-offset").onchange = async event => {
    await applySelectedSync(Math.round(Number(event.target.value) * 1000), Number($("#sync-rate").value));
  };
  $("#sync-rate").onchange = async event => {
    const ratePpm = Math.round(Number(event.target.value));
    if (ratePpm && !await confirmAction("Rate correction changes timing fidelity and will be disclosed in the manifest. Apply it?", {title: "Apply rate correction", confirmLabel: "Apply correction"})) {
      renderSourceInspector();
      return;
    }
    await applySelectedSync(Math.round(Number($("#sync-offset").value) * 1000), ratePpm);
  };
  async function applySelectedSync(anchorAlignedUs, ratePpm) {
    const source = state.sources[state.selectedSource];
    const clip = selectedAlignmentClip(source);
    if (!clip) return;
    const alignment = {...clipAlignment(clip), anchorAlignedUs, ratePpm};
    const preview = await command("SetClipAlignment", {clipId: clip.id, alignment, confirmDrift: Boolean(alignment.ratePpm)}, {preview: true});
    if (!preview) return;
    const introduced = preview.issues.filter(issue => !state.compiled.issues.some(previous => previous.id === issue.id));
    if (introduced.length && !await confirmAction(`This alignment introduces ${introduced.length} new canonical issue(s). Apply it?`, {title: "Apply alignment with issues", confirmLabel: "Apply alignment"})) return;
    await command("SetClipAlignment", {clipId: clip.id, alignment, confirmDrift: Boolean(alignment.ratePpm)});
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
  $("#confirm-source-identities").onclick = async () => {
    const sourceIds = state.preparation?.sourceIdentity?.provisionalSourceIds || [];
    if (!sourceIds.length) return;
    if (!await confirmAction(`Confirm these ${sourceIds.length} proposed source tracks as camera/viewpoint identities? You can still merge, split, rename, and reassign clips later.`, {title: "Confirm source identities", confirmLabel: "Confirm sources"})) return;
    if (await command("ConfirmSourceIdentities", {sourceIds})) {
      toast("Source identities confirmed as an explicit project decision");
    }
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
    if (source && targetSourceId && await confirmAction(`Merge ${source.label} into the selected destination? This remains a revisioned project decision.`, {title: "Merge source identities", confirmLabel: "Merge sources"})) await command("MergeLogicalSources", {targetSourceId, sourceIds: [source.id]});
  };
  $("#archive-source").onclick = async () => {
    const source = state.sources[state.selectedSource];
    if (source) await command("ArchiveLogicalSource", {sourceId: source.id, archived: true});
  };
  $("#analyze-alignment").onclick = async () => {
    try {
      const job = await client.startAlignmentAnalysis({projectId: state.project.id}, {});
      await pollJob(job.id, value => { $("#footer-status").textContent = `${value.status} · ${value.message}`; });
      await refreshPreparationState();
      renderPreparation();
      renderSources();
      renderSuggestions();
    } catch (error) { handleError(error); }
  };
  $("#build-first-cut").onclick = buildFirstCut;
  $$('input[name="anchor"]').forEach(radio => {
    radio.onchange = async () => {
      const anchorMode = radio.value === "source-clips" ? "SOURCE_TIME" : "PROGRAM_TIME";
      const preview = await command("SetAnchoringMode", {anchorMode}, {preview: true});
      if (preview && await confirmAction(anchorMode === "SOURCE_TIME" ? "Attach future timing edits to source-relative points? Alignment changes may move output boundaries." : "Keep future program boundaries fixed on the output clock?", {title: "Change cut anchoring", confirmLabel: "Change anchoring"})) {
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
  $("#delete-video").onclick = async () => {
    const block = currentVideoBlock();
    if (block && await confirmAction("Delete this video decision and expose the resulting coverage issue?", {title: "Delete video decision", confirmLabel: "Delete decision"})) command("DeleteVideoBlock", {blockId: block.id});
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
  $("#delete-audio").onclick = async () => {
    const block = currentAudioBlock();
    if (block && await confirmAction("Delete this audio decision and expose the resulting issue?", {title: "Delete audio decision", confirmLabel: "Delete decision"})) command("DeleteAudioBlock", {blockId: block.id});
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
  $$('[data-action="rewind"]').forEach(button => { button.onclick = () => setPlayhead(state.playhead - (1_000_000_000 / activeClockDurationUs())); });
  $$('[data-action="forward"]').forEach(button => { button.onclick = () => setPlayhead(state.playhead + (1_000_000_000 / activeClockDurationUs())); });
  $(".scrubber").oninput = event => setPlayhead(Number(event.target.value));
  $("#timeline-zoom").oninput = event => {
    clearTimeout(changeTimelineZoom.timer);
    changeTimelineZoom.timer = setTimeout(() => changeTimelineZoom(Number(event.target.value)).catch(handleError), 80);
  };
  $("#timeline-zoom-out").onclick = () => changeTimelineZoom(state.timelineZoom - 10).catch(handleError);
  $("#timeline-zoom-in").onclick = () => changeTimelineZoom(state.timelineZoom + 10).catch(handleError);
  $("#timeline-fit").onclick = () => changeTimelineZoom(0, (state.evidenceStartUs + state.evidenceEndUs) / 2).catch(handleError);
  $("#center-playhead").onclick = () => changeTimelineZoom(state.timelineZoom, currentAlignedUs()).catch(handleError);
  $("#timeline-pan").oninput = event => queueTimelinePan(Number(event.target.value) / 1000);
  const alignmentTimeline = $("#alignment-timeline");
  alignmentTimeline.onwheel = event => {
    const horizontalDelta = Math.abs(event.deltaX) >= Math.abs(event.deltaY) ? event.deltaX : event.shiftKey ? event.deltaY : 0;
    if (!horizontalDelta || state.timelineZoom <= 0) return;
    event.preventDefault();
    const current = Number($("#timeline-pan").value) / 1000;
    queueTimelinePan(current + horizontalDelta / 1800);
  };
  alignmentTimeline.onkeydown = event => {
    if (event.target !== alignmentTimeline || state.timelineZoom <= 0) return;
    const current = Number($("#timeline-pan").value) / 1000;
    const movement = {ArrowLeft: -0.08, ArrowRight: 0.08, PageUp: -0.4, PageDown: 0.4};
    if (event.key === "Home" || event.key === "End" || movement[event.key]) {
      event.preventDefault();
      queueTimelinePan(event.key === "Home" ? 0 : event.key === "End" ? 1 : current + movement[event.key]);
    }
  };
  $("#reviewed-check").onchange = updateRenderButton;
  $("#render-video").onclick = renderVideo;
  $("#output-path").oninput = () => {
    if (state.renderPlan) {
      $("#preflight-heading").textContent = "Reusable program ready";
      $("#preflight-status").innerHTML = "<strong>Output settings are mutable</strong><p>The reviewed cut and source hashes will be reused for this output.</p>";
      return;
    }
    invalidateReviewPreparation();
    clearTimeout(prepareReview.inputTimer);
    prepareReview.inputTimer = setTimeout(() => { if (state.view === "review") prepareReview(); }, 350);
  };
  $("#output-path").onblur = () => {
    clearTimeout(prepareReview.inputTimer);
    if (state.view === "review" && !state.renderPlan) prepareReview({provisionGrant: true});
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
    if (event.key === "ArrowLeft" && event.altKey) setPlayhead(state.playhead - (10_000_000 / activeClockDurationUs()));
    if (event.key === "ArrowRight" && event.altKey) setPlayhead(state.playhead + (10_000_000 / activeClockDurationUs()));
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
  syncSourceMonitors();
}

function renderTimelineTracks() {
  $("#alignment-tracks").innerHTML = state.sources.map(source => trackMarkup(source)).join("");
  $("#source-evidence-tracks").innerHTML = `<div class="timeline-shell">${state.sources.map(source => trackMarkup(source)).join("")}</div>`;
  bindAlignmentTrackInteractions();
  if (state.monitorPoint) applySourceMonitorPoint(state.monitorPoint, state.playing);
}

function bindAlignmentTrackInteractions() {
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
        const deltaUs = ((moveEvent.clientX - startX) / width) * Math.max(1, state.timelineEndUs - state.timelineStartUs);
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
}

function handleError(error) {
  if (error instanceof APIError && error.code === "UNAUTHENTICATED") {
    showSessionRecovery();
    return;
  }
  const label = error instanceof APIError ? `${error.code}: ${error.message}` : error.message || String(error);
  toast(label);
}

function showSessionRecovery() {
  $("#footer-status").textContent = "Secure session expired · reopen required";
  const dialog = $("#session-recovery-dialog");
  if (dialog && !dialog.open) dialog.showModal();
}

$("#copy-session-recovery").onclick = async () => {
  const command = $("#session-recovery-command").textContent;
  try {
    await navigator.clipboard.writeText(command);
    $("#copy-session-recovery").textContent = "Copied";
  } catch (_error) {
    window.getSelection()?.selectAllChildren($("#session-recovery-command"));
    toast("Command selected; press Command-C to copy it");
  }
};

$("#retry-session").onclick = () => window.location.reload();

/**
 * Initializes the secure application session and loads the initial library data.
 * Falls back to default settings if application settings cannot be loaded and reports launch errors in the footer.
 */
async function start() {
  try {
    const session = await client.getSession();
    $("#session-recovery-command").textContent = session.recoveryCommand;
    try {
      state.settings = await client.getApplicationSettings();
    } catch (error) {
      console.warn("Using default application settings", error);
    }
    applyAppearanceSettings();
    $("#library-time-zone").value = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    setupEvents();
    connectEventFeed();
    await loadLibraries();
    $("#footer-status").textContent = "Secure local session ready";
  } catch (error) {
    handleError(error);
  }
}

start();
