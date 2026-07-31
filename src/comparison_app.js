(() => {
  'use strict'

  const $ = (selector, root = document) => root.querySelector(selector)
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)]
  const escapeHTML = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[char])
  const statusLabels = { unreviewed: '待审核', match: '基本一致', partial: '部分成立', difference: '存在偏差', uncertain: '无法确认' }
  const humanStatusLabels = { unreviewed: '待审核', match: '复刻到位', partial: '部分到位', difference: '关键内容缺失', uncertain: '无法确认' }
  const feedbackKindLabels = { confirmed: '确认到位', missing: 'AI 漏识别', incorrect: 'AI 写错' }
  const feedbackAxisLabels = { camera_geometry: '镜头形态', action_causality: '动作因果', composition_space: '构图空间', timing_edit: '节奏剪辑', vfx_interaction: '特效交互', character_continuity: '角色连续性', other: '其他' }
  const severityLabels = { high: '高优先', medium: '中优先', low: '低优先', none: '接近' }
  const bootstrap = window.LAPIAN_COMPARISON || {}
  const bootstrapHasAudit = Boolean(bootstrap.audit_bundle?.review_status)
  const bootstrapHasEvents = Array.isArray(bootstrap.event_segments?.events) && bootstrap.event_segments.events.length > 0
  let comparison = bootstrap
  let selectedPairId = ''
  let selectedEventId = ''
  let activeTab = bootstrapHasEvents ? 'events' : bootstrapHasAudit ? 'audit' : 'shots'
  let monitorSelection = { type: bootstrapHasEvents ? 'event' : 'pair', id: '' }
  let monitorWindow = null
  let auditFilter = 'all'
  let saveTimer = null
  let pollTimer = null
  let toastTimer = null
  let reviewDirty = false
  let saving = false
  let currentPhase = 0
  let abSide = 'source'
  let syncing = false
  let lastStatus = bootstrap.status || ''

  const projectId = () => comparison.project_id || bootstrap.project_id || ''
  const comparisonId = () => comparison.comparison_id || bootstrap.comparison_id || ''
  const apiBase = () => `/api/projects/${encodeURIComponent(projectId())}/comparisons/${encodeURIComponent(comparisonId())}`
  const pairs = () => comparison.alignment?.pairs || []
  const events = () => comparison.event_segments?.events || []
  const auditBundle = () => comparison.audit_bundle && typeof comparison.audit_bundle === 'object' ? comparison.audit_bundle : null
  const selectedPair = () => pairs().find((pair) => pair.pair_id === selectedPairId) || pairs()[0] || null
  const selectedEvent = () => events().find((event) => event.event_id === selectedEventId) || events()[0] || null
  const playbackUnit = () => {
    if (monitorSelection.type === 'event') return events().find((event) => event.event_id === monitorSelection.id) || selectedEvent()
    return pairs().find((pair) => pair.pair_id === monitorSelection.id) || selectedPair()
  }

  function mediaURL(path) {
    if (!path) return ''
    const encoded = String(path).split('/').filter(Boolean).map(encodeURIComponent).join('/')
    return `/projects/${encodeURIComponent(projectId())}/${encoded}`
  }

  function formatTime(value, compact = false) {
    const seconds = Math.max(0, Number(value || 0))
    const minutes = Math.floor(seconds / 60)
    const remainder = seconds - minutes * 60
    if (compact) return `${String(minutes).padStart(2, '0')}:${remainder.toFixed(3).padStart(6, '0')}`
    const hours = Math.floor(minutes / 60)
    return `${String(hours).padStart(2, '0')}:${String(minutes % 60).padStart(2, '0')}:${remainder.toFixed(3).padStart(6, '0')}`
  }

  function signedSeconds(value) {
    const number = Number(value || 0)
    return `${number > 0 ? '+' : ''}${number.toFixed(3)}s`
  }

  function signedPercent(value) {
    const number = Number(value || 0) * 100
    return `${number > 0 ? '+' : ''}${number.toFixed(1)}%`
  }

  function toast(message) {
    const element = $('#toast')
    element.textContent = message
    element.classList.add('visible')
    clearTimeout(toastTimer)
    toastTimer = setTimeout(() => element.classList.remove('visible'), 2500)
  }

  function setSaveState(text, tone = '') {
    const element = $('#saveState')
    element.textContent = text
    element.style.color = tone === 'error' ? 'var(--danger)' : tone === 'success' ? 'var(--success)' : ''
  }

  function ensureReview() {
    if (!comparison.review || typeof comparison.review !== 'object') comparison.review = {}
    if (!comparison.review.event_reviews) comparison.review.event_reviews = {}
    if (!comparison.review.pair_reviews) comparison.review.pair_reviews = {}
    if (!Array.isArray(comparison.review.technical_identifications)) comparison.review.technical_identifications = []
    if (!Array.isArray(comparison.review.fix_queue)) comparison.review.fix_queue = []
    if (typeof comparison.review.overall_note !== 'string') comparison.review.overall_note = ''
    return comparison.review
  }

  function ensureEventReview(eventId) {
    const review = ensureReview()
    if (!review.event_reviews[eventId]) {
      const seededSeverity = events().find((event) => event.event_id === eventId)?.analysis?.severity
      review.event_reviews[eventId] = {
        status: 'unreviewed', severity: ['high', 'medium', 'low', 'none'].includes(seededSeverity) ? seededSeverity : 'medium', observed_difference: '', impact: '', root_cause: '', next_action: '', conclusion: '', stage_reviews: {},
      }
    }
    const eventReview = review.event_reviews[eventId]
    if (!eventReview.stage_reviews) eventReview.stage_reviews = {}
    if (!['unreviewed', 'match', 'partial', 'difference', 'uncertain'].includes(eventReview.human_status)) eventReview.human_status = 'unreviewed'
    if (typeof eventReview.human_summary !== 'string') eventReview.human_summary = ''
    if (!Array.isArray(eventReview.feedback_entries)) eventReview.feedback_entries = []
    return eventReview
  }

  function ensurePairReview(pairId) {
    const review = ensureReview()
    if (!review.pair_reviews[pairId]) review.pair_reviews[pairId] = { dimensions: {}, node_reviews: {}, conclusion: '' }
    const pairReview = review.pair_reviews[pairId]
    if (!pairReview.dimensions) pairReview.dimensions = {}
    if (!pairReview.node_reviews) pairReview.node_reviews = {}
    return pairReview
  }

  function markDirty() {
    reviewDirty = true
    setSaveState('有未保存修改')
    clearTimeout(saveTimer)
    saveTimer = setTimeout(() => saveReview(false), 900)
  }

  async function saveReview(notify = true) {
    if (saving || (!reviewDirty && !notify)) return false
    saving = true
    clearTimeout(saveTimer)
    setSaveState('正在保存…')
    try {
      const response = await fetch(`${apiBase()}/review`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ review: ensureReview() }),
      })
      const data = await response.json().catch(() => ({}))
      if (!response.ok || !data.comparison) throw new Error(data.error || `保存失败：HTTP ${response.status}`)
      comparison.review = data.comparison.review || comparison.review
      reviewDirty = false
      setSaveState('审核已保存', 'success')
      if (notify) toast('审核记录已保存到本地项目')
      return true
    } catch (error) {
      setSaveState('保存失败', 'error')
      if (notify) toast(error.message || '保存失败')
      return false
    } finally {
      saving = false
    }
  }

  function statusBanner() {
    const ready = comparison.status === 'ready'
    $('#statusBanner').classList.toggle('visible', !ready)
    $('#statusStage').textContent = comparison.stage || (ready ? '' : '正在处理')
    $('#statusDetail').textContent = comparison.progress_detail || comparison.error || ''
    $('#statusProgress').textContent = `${Math.round(Number(comparison.progress || 0))}%`
    $('#alignmentButton').disabled = !ready
    $('#saveReviewButton').disabled = !ready
  }

  function renderSummary() {
    const summary = comparison.summary || {}
    const eventItems = events()
    const pendingEvents = eventItems.filter((event) => ensureEventReview(event.event_id).human_status === 'unreviewed').length
    const feedbackCount = eventItems.reduce((total, event) => total + ensureEventReview(event.event_id).feedback_entries.length, 0)
    $('#pageTitle').textContent = comparison.title || '复刻对比'
    $('#pageSubtitle').textContent = comparison.reference?.title || ''
    $('#comparisonTitle').textContent = comparison.title || '复刻对比'
    $('#comparisonMeta').textContent = `${comparison.reference?.title || '原片'} ↔ ${comparison.remake?.title || '复刻片'} · ${comparison.alignment?.mode === 'manual' ? '人工对应' : '自动对应待复核'}`
    $('#shotCountMetric').textContent = `${summary.reference_shot_count ?? '—'} / ${summary.remake_shot_count ?? '—'}`
    $('#durationMetric').textContent = Number.isFinite(Number(summary.duration_delta_sec)) ? signedSeconds(summary.duration_delta_sec) : '—'
    $('#unmatchedMetric').textContent = `${summary.missing_shot_count || 0} / ${summary.extra_shot_count || 0}`
    $('#findingMetricLabel').textContent = eventItems.length ? '待审段 / 人工反馈' : '机械差异'
    $('#findingMetric').textContent = eventItems.length ? `${pendingEvents} / ${feedbackCount}` : summary.finding_count ?? '—'
    $('#unmatchedMetric').parentElement.classList.toggle('is-alert', Boolean((summary.missing_shot_count || 0) + (summary.extra_shot_count || 0)))
    $('#findingMetric').parentElement.classList.toggle('is-alert', eventItems.length ? Boolean(pendingEvents) : Boolean(summary.high_finding_count))
  }

  function setVideoSource(video, path) {
    const url = mediaURL(path)
    if (!url || video.dataset.path === path) return
    video.dataset.path = path
    video.src = url
    video.load()
  }

  function initializeVideos() {
    setVideoSource($('#sourceVideo'), comparison.reference?.video_path)
    setVideoSource($('#remakeVideo'), comparison.remake?.video_path)
    const orientation = comparison.reference?.orientation || comparison.remake?.orientation || 'landscape'
    $('#monitor').dataset.orientation = orientation
    updateAudioMode()
  }

  function pairSeverity(pair) {
    if (!pair) return 'none'
    if (pair.status !== 'matched') return pair.status
    const findingMap = new Map((comparison.findings || []).map((finding) => [finding.finding_id, finding]))
    const rank = { high: 3, medium: 2, low: 1, none: 0 }
    return (pair.finding_ids || []).map((id) => findingMap.get(id)?.severity || 'none').sort((a, b) => rank[b] - rank[a])[0] || 'none'
  }

  function pairLabel(pair) {
    const source = pair.source?.shot_id || '—'
    const remake = pair.remake?.shot_id || '—'
    return `${source} ↔ ${remake}`
  }

  function sideRange(unit, side) {
    const shot = monitorWindow?.[side] || unit?.[side]
    if (!shot) return null
    return { start: Number(shot.start_sec || 0), end: Number(shot.end_sec || 0), duration: Math.max(.001, Number(shot.duration_sec || 0)) }
  }

  function setTimeWhenReady(video, value) {
    const set = () => {
      try { video.currentTime = Math.max(0, value) } catch {}
    }
    if (video.readyState >= 1) set()
    else video.addEventListener('loadedmetadata', set, { once: true })
  }

  function seekPhase(value) {
    const unit = playbackUnit()
    if (!unit) return
    currentPhase = Math.max(0, Math.min(1, Number(value || 0)))
    const source = sideRange(unit, 'source')
    const remake = sideRange(unit, 'remake')
    syncing = true
    if (source) setTimeWhenReady($('#sourceVideo'), source.start + source.duration * currentPhase)
    if (remake) setTimeWhenReady($('#remakeVideo'), remake.start + remake.duration * currentPhase)
    syncing = false
    $('#phaseSlider').value = String(Math.round(currentPhase * 1000))
    updateTimeReadout()
  }

  function updateTimeReadout() {
    const unit = playbackUnit()
    if (!unit) { $('#timeReadout').textContent = '00:00.000 / 00:00.000'; return }
    const range = sideRange(unit, unit.source ? 'source' : 'remake')
    if (!range) return
    $('#timeReadout').textContent = `${formatTime(range.duration * currentPhase, true)} / ${formatTime(range.duration, true)}`
  }

  function applyPlaybackRates() {
    const unit = playbackUnit()
    if (!unit) return
    const base = Number($('#speedSelect').value || 1)
    const source = sideRange(unit, 'source')
    const remake = sideRange(unit, 'remake')
    $('#sourceVideo').playbackRate = base
    $('#remakeVideo').playbackRate = base
    if ($('#syncMode').value === 'phase' && source && remake) {
      $('#remakeVideo').playbackRate = Math.max(.1, Math.min(4, base * remake.duration / source.duration))
    }
  }

  function pauseAll() {
    $('#sourceVideo').pause()
    $('#remakeVideo').pause()
    $('#playButton').textContent = '▶'
    $('#playButton').setAttribute('aria-label', monitorSelection.type === 'event' ? '播放所选事件段' : '播放所选镜头')
  }

  async function togglePlayback() {
    const unit = playbackUnit()
    if (!unit) return
    const sourceVideo = $('#sourceVideo')
    const remakeVideo = $('#remakeVideo')
    const activeVideos = [unit.source ? sourceVideo : null, unit.remake ? remakeVideo : null].filter(Boolean)
    if (activeVideos.some((video) => !video.paused)) { pauseAll(); return }
    if (currentPhase >= .995) seekPhase(0)
    applyPlaybackRates()
    const results = await Promise.allSettled(activeVideos.map((video) => video.play()))
    if (results.some((result) => result.status === 'fulfilled')) {
      $('#playButton').textContent = 'Ⅱ'
      $('#playButton').setAttribute('aria-label', '暂停所选镜头')
    }
  }

  function syncFromMaster(event) {
    if (syncing) return
    const unit = playbackUnit()
    if (!unit) return
    const source = sideRange(unit, 'source')
    const remake = sideRange(unit, 'remake')
    const masterSide = source ? 'source' : 'remake'
    const masterVideo = masterSide === 'source' ? $('#sourceVideo') : $('#remakeVideo')
    if (event.currentTarget !== masterVideo) return
    const masterRange = masterSide === 'source' ? source : remake
    if (!masterRange) return
    currentPhase = Math.max(0, Math.min(1, (masterVideo.currentTime - masterRange.start) / masterRange.duration))
    $('#phaseSlider').value = String(Math.round(currentPhase * 1000))
    updateTimeReadout()
    const otherVideo = masterSide === 'source' ? $('#remakeVideo') : $('#sourceVideo')
    const otherRange = masterSide === 'source' ? remake : source
    if (otherRange && !masterVideo.paused) {
      const expected = $('#syncMode').value === 'phase'
        ? otherRange.start + otherRange.duration * currentPhase
        : otherRange.start + Math.min(otherRange.duration, masterRange.duration * currentPhase)
      if (Math.abs(otherVideo.currentTime - expected) > .14) {
        syncing = true
        try { otherVideo.currentTime = expected } catch {}
        syncing = false
      }
    }
    if (currentPhase >= .998 || masterVideo.currentTime >= masterRange.end - .02) {
      if ($('#loopToggle').checked) {
        seekPhase(0)
        applyPlaybackRates()
        Promise.allSettled([unit.source ? $('#sourceVideo').play() : null, unit.remake ? $('#remakeVideo').play() : null].filter(Boolean))
      } else pauseAll()
    }
  }

  function updateAudioMode() {
    const mode = $('#audioMode').value
    $('#sourceVideo').muted = !['source', 'both'].includes(mode)
    $('#remakeVideo').muted = !['remake', 'both'].includes(mode)
    $('#sourceVideo').volume = mode === 'both' ? .6 : 1
    $('#remakeVideo').volume = mode === 'both' ? .6 : 1
  }

  function renderMonitorSelection() {
    const unit = playbackUnit()
    if (!unit) return
    if (monitorSelection.type === 'event') {
      $('#monitorPairTitle').textContent = `${unit.event_id} · ${unit.title}`
      $('#monitorPairMeta').textContent = monitorWindow?.label
        ? `${monitorWindow.label} · 前后连续时间窗`
        : `关键事件段 · 原片 ${unit.source.duration_sec.toFixed(3)}s · 复刻 ${unit.remake.duration_sec.toFixed(3)}s`
    } else {
      const duration = unit.metrics?.duration_delta_sec
      $('#monitorPairTitle').textContent = `${unit.pair_id} · ${pairLabel(unit)}`
      $('#monitorPairMeta').textContent = unit.status === 'matched'
        ? `机械切镜 · 原片 ${unit.source.duration_sec.toFixed(3)}s · 复刻 ${unit.remake.duration_sec.toFixed(3)}s · 时长差 ${signedSeconds(duration)}`
        : unit.status === 'missing' ? '机械切镜 · 复刻片缺少对应镜头' : '机械切镜 · 复刻片存在额外镜头'
    }
    $('#sourcePlayer').classList.toggle('empty', !unit.source)
    $('#remakePlayer').classList.toggle('empty', !unit.remake)
    pauseAll()
    seekPhase(0)
  }

  function selectPair(pairId, { scroll = false } = {}) {
    const pair = pairs().find((item) => item.pair_id === pairId)
    if (!pair) return
    selectedPairId = pairId
    monitorSelection = { type: 'pair', id: pairId }
    monitorWindow = null
    history.replaceState(null, '', `#${encodeURIComponent(pairId)}`)
    renderMonitorSelection()
    if (activeTab === 'shots') renderActiveTab()
    else $$('.timeline-shot,.metrics-table tr').forEach((element) => element.classList.toggle('on', element.dataset.pairId === pairId))
    if (scroll) $('#monitor').scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' })
  }

  function selectEvent(eventId, { scroll = false } = {}) {
    const event = events().find((item) => item.event_id === eventId)
    if (!event) return
    selectedEventId = eventId
    monitorSelection = { type: 'event', id: eventId }
    monitorWindow = null
    history.replaceState(null, '', `#${encodeURIComponent(eventId)}`)
    renderMonitorSelection()
    if (activeTab === 'events') renderActiveTab()
    if (scroll) $('#monitor').scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' })
  }

  function selectAdjacent(offset) {
    const list = monitorSelection.type === 'event' ? events() : pairs()
    const key = monitorSelection.type === 'event' ? 'event_id' : 'pair_id'
    const selectedId = monitorSelection.type === 'event' ? selectedEventId : selectedPairId
    const index = Math.max(0, list.findIndex((item) => item[key] === selectedId))
    const next = list[Math.max(0, Math.min(list.length - 1, index + offset))]
    if (next && monitorSelection.type === 'event') selectEvent(next.event_id)
    else if (next) selectPair(next.pair_id)
  }

  function reviewOptions(selected) {
    return Object.entries(statusLabels).map(([value, label]) => `<option value="${value}" ${selected === value ? 'selected' : ''}>${label}</option>`).join('')
  }

  function frameMarkup(side, label) {
    if (!side) return `<figure class="evidence-frame"><figcaption><span>${label}</span><span>无对应证据</span></figcaption><div class="frame-missing">该侧没有对应节点</div></figure>`
    const signals = side.signals || {}
    const signalItems = Object.keys(signals).length
      ? `<details class="signal-details"><summary>机器信号</summary><div class="signal-row"><span>运动 ${Number(signals.motion || 0).toFixed(2)}</span><span>亮度变化 ${Number(signals.light_change || 0).toFixed(2)}</span><span>声音能量 ${Number(signals.audio || 0).toFixed(2)}</span></div></details>`
      : ''
    const image = side.image ? `<img src="${mediaURL(side.image)}" alt="${escapeHTML(label)} ${escapeHTML(side.timecode || '')}" loading="lazy">` : '<div class="frame-missing">没有生成该节点帧图</div>'
    return `<figure class="evidence-frame"><figcaption><span>${label}</span><span>${escapeHTML(side.timecode || '')} · ${Math.round(Number(side.phase || 0) * 100)}%</span></figcaption>${image}${signalItems}</figure>`
  }

  function nodeMarkup(pair, node) {
    const pairReview = ensurePairReview(pair.pair_id)
    const review = pairReview.node_reviews[node.node_id] || {}
    const phaseText = node.phase_delta_percent == null ? '未对应' : `相位差 ${Number(node.phase_delta_percent) > 0 ? '+' : ''}${Number(node.phase_delta_percent).toFixed(1)}%`
    const seekPhaseValue = Number(node.source?.phase ?? node.remake?.phase ?? 0)
    return `<article class="node">
      <div class="node-head"><span class="node-kind">${escapeHTML(node.conclusion_level || '待核')}</span><b>${escapeHTML(node.label)}</b><span class="node-time">${escapeHTML(phaseText)}</span><button class="seek-node" type="button" data-seek-phase="${seekPhaseValue}">定位播放</button></div>
      <div class="frame-pair">${frameMarkup(node.source, 'A · 原片')}${frameMarkup(node.remake, 'B · 复刻')}</div>
      <div class="node-review">
        <div class="field"><label>节点判断</label><select data-node-field="status" data-node-id="${node.node_id}">${reviewOptions(review.status || 'unreviewed')}</select></div>
        <div class="field"><label>专业节点名称</label><input data-node-field="event_label" data-node-id="${node.node_id}" value="${escapeHTML(review.event_label || '')}" placeholder="如：启动、接触、重击峰值、反应、收势"></div>
        <div class="field"><label>具体差异与动态细节</label><textarea data-node-field="note" data-node-id="${node.node_id}" placeholder="描述前因、动作过程、画面结果和下一节点桥接…">${escapeHTML(review.note || '')}</textarea></div>
      </div>
    </article>`
  }

  function dimensionMarkup(pair, dimension) {
    const pairReview = ensurePairReview(pair.pair_id)
    const review = pairReview.dimensions[dimension.key] || {}
    return `<div class="dimension-row">
      <div class="dimension-name"><b>${escapeHTML(dimension.label)}</b><span>${escapeHTML(dimension.automatic_scope)}</span></div>
      <select data-dimension-field="status" data-dimension="${dimension.key}">${reviewOptions(review.status || 'unreviewed')}</select>
      <div class="dimension-note"><label>事实差异、观感影响与修改方向</label><textarea data-dimension-field="note" data-dimension="${dimension.key}" placeholder="先写看见的事实，再写影响和下一版怎么改…">${escapeHTML(review.note || '')}</textarea></div>
    </div>`
  }

  function eventStageMarkup(event, stage) {
    const eventReview = ensureEventReview(event.event_id)
    const stageReview = eventReview.stage_reviews[stage.stage_id] || {}
    const status = stageReview.status || stage.status || 'unreviewed'
    const sourceRange = `${formatTime(stage.source?.start_sec, true)}—${formatTime(stage.source?.end_sec, true)}`
    const remakeRange = `${formatTime(stage.remake?.start_sec, true)}—${formatTime(stage.remake?.end_sec, true)}`
    return `<div class="event-stage">
      <div class="stage-name"><b>${escapeHTML(stage.label)}</b><span>${escapeHTML(sourceRange)} / ${escapeHTML(remakeRange)}</span></div>
      <div class="stage-copy"><b>原片必须成立</b><p>${escapeHTML(stage.source_description || '结合连续画面确认这一阶段的进入状态、动作关系和结果。')}</p></div>
      <div class="stage-copy remake"><b>AI 复刻观察</b><p>${escapeHTML(stage.remake_observation || stageReview.note || '待人工复核连续时间窗。')}</p></div>
      <div class="stage-actions"><span class="stage-status ${escapeHTML(status)}">${escapeHTML(statusLabels[status] || status)}</span><button class="stage-seek" type="button" data-event-window="${event.event_id}" data-stage-id="${stage.stage_id}">播放此窗口</button></div>
    </div>`
  }

  function eventReviewField(eventId, field, label, value, placeholder) {
    return `<div class="field wide"><label>${escapeHTML(label)}</label><textarea data-event-field="${field}" data-event-id="${eventId}" placeholder="${escapeHTML(placeholder)}">${escapeHTML(value || '')}</textarea></div>`
  }

  function auditAttachment(label, path) {
    return path ? `<a class="quiet-button" href="${mediaURL(path)}" target="_blank" rel="noopener">${escapeHTML(label)}</a>` : ''
  }

  function renderAudit() {
    const audit = auditBundle()
    if (!audit) return '<div class="empty-state"><b>没有完整审查结论</b><span>该比较仍可使用事件段、机械切镜和修复队列。</span></div>'
    const verdict = audit.verdict || {}
    const contract = audit.prompt_contract || {}
    const categories = audit.classification_categories || []
    const shots = audit.shots || []
    const visibleShots = auditFilter === 'all' ? shots : shots.filter((shot) => shot.classification === auditFilter)
    const categoryButtons = [
      `<button class="${auditFilter === 'all' ? 'on' : ''}" type="button" data-audit-filter="all">全部 ${shots.length}</button>`,
      ...categories.map((category) => `<button class="${auditFilter === category.key ? 'on' : ''}" type="button" data-audit-filter="${escapeHTML(category.key)}">${escapeHTML(category.label)} ${category.count}</button>`),
    ].join('')
    const shotGrid = visibleShots.map((shot) => `<button class="audit-shot ${escapeHTML(shot.tone || '')} ${shot.critical ? 'critical' : ''}" type="button" data-audit-shot="${escapeHTML(shot.shot_id)}" title="${escapeHTML(`${shot.shot_id} ${shot.title || ''} · ${shot.classification_label || ''}`)}"><b>${escapeHTML(shot.shot_id)}</b><span>${escapeHTML(shot.classification_label || '')}</span></button>`).join('')
    const globalRows = (audit.global_findings || []).map((finding) => `<div class="audit-finding-row"><span class="severity ${escapeHTML(finding.tone || 'medium')}">${escapeHTML(finding.severity || '')}</span><div class="audit-finding-axis"><b>${escapeHTML(finding.axis_label || finding.axis || '')}</b><span>${escapeHTML(finding.id || '')}</span></div><div class="audit-finding-copy"><b>${escapeHTML(finding.finding || '')}</b><p>${escapeHTML(finding.evidence || '')}</p><div class="audit-repair"><b>修改：</b>${escapeHTML(finding.repair || '')}</div></div></div>`).join('')
    const criticalRows = shots.filter((shot) => shot.critical).map((shot) => {
      const image = shot.evidence_sheet ? `<img src="${mediaURL(shot.evidence_sheet)}" alt="${escapeHTML(shot.shot_id)} 双轴逐镜证据" loading="lazy">` : ''
      return `<article class="audit-critical"><div class="audit-critical-id"><b>${escapeHTML(shot.shot_id)}</b><span>${escapeHTML(shot.severity || '')} · ${escapeHTML(shot.segment_id || '')}</span></div><div class="audit-critical-copy"><b>${escapeHTML(shot.title || '关键镜头')}</b><p>${escapeHTML(shot.finding || '')}</p><p class="audit-repair"><b>修改：</b>${escapeHTML(shot.repair || '')}</p></div><div class="audit-critical-media">${image}<button class="quiet-button" type="button" data-audit-shot="${escapeHTML(shot.shot_id)}">定位双轴时间窗</button></div></article>`
    }).join('')
    const promptRows = (contract.segments || []).map((segment) => {
      const risk = segment.execution_risk || 'unknown'
      const riskLabel = { high: '高风险', medium: '中风险', lower: '较低风险' }[risk] || '未判定'
      return `<tr><td><b>${escapeHTML(segment.segment_id || '')}</b></td><td>${escapeHTML(segment.title || '')}</td><td>${Number(segment.duration_sec || 0).toFixed(3)}s</td><td>${segment.shot_count || 0}</td><td>${segment.unicode_characters || 0}</td><td><span class="audit-risk ${escapeHTML(risk)}">${riskLabel}</span></td></tr>`
    }).join('')
    const limitations = (audit.limitations || []).map((item) => `<li>${escapeHTML(item)}</li>`).join('')
    const attachments = audit.attachments || {}
    const metricByTone = new Map(categories.map((category) => [category.tone, category]))
    const keep = metricByTone.get('keep') || {}
    const repair = metricByTone.get('repair') || {}
    const rebuild = metricByTone.get('rebuild') || {}
    const highRisk = (contract.high_risk_segments || []).join(' ')
    return `<section class="wide-view">
      <div class="wide-head"><div><h1>完整人工审查结论</h1><p>双轴时间码、十段人工锚点、逐镜分流与提示词合同使用同一份审查数据。</p></div></div>
      <div class="audit-verdict"><div class="audit-verdict-state"><b>${escapeHTML(verdict.label || '待判定')}</b><span>${escapeHTML(audit.review_date || '')} · 人工复核完成</span></div><div class="audit-verdict-copy"><b>可用边界：${escapeHTML(verdict.usable_as || '')}</b><p>${escapeHTML(verdict.summary || '')}</p></div><div class="audit-actions">${auditAttachment('完整报告', attachments.report)}${auditAttachment('逐镜数据', attachments.findings)}${auditAttachment('提示词合同', attachments.prompt_contract)}</div></div>
      <div class="audit-metrics"><div class="audit-metric"><span>可保留并小修</span><b>${keep.count || 0} 镜</b><small>保留主体内容后重剪或小修</small></div><div class="audit-metric"><span>局部修复</span><b>${repair.count || 0} 镜</b><small>定向重生成或局部后期</small></div><div class="audit-metric"><span>重做或补插帧</span><b>${rebuild.count || 0} 镜</b><small>动作因果或镜头任务未成立</small></div><div class="audit-metric"><span>提示词合同</span><b>${contract.checks_passed || 0} / ${contract.checks_total || 0}</b><small>高执行风险 ${escapeHTML(highRisk || '无')}</small></div></div>
      <section class="section-block"><div class="section-head"><h2>全片级错误</h2><span>${(audit.global_findings || []).length} 项人工确认</span></div><div class="audit-finding-list">${globalRows}</div></section>
      <section class="section-block"><div class="section-head"><h2>七十七镜分流</h2><span>镜号对应双轴时间窗</span><div class="audit-filter" role="group" aria-label="镜头处理分类">${categoryButtons}</div></div><div class="audit-shot-grid">${shotGrid}</div><p class="audit-note">关键镜头使用密集帧人工确认范围；其余镜头按十段人工锚点做单调插值，仅用于回看定位。</p></section>
      <section class="section-block"><div class="section-head"><h2>关键镜头证据与修改动作</h2><span>${shots.filter((shot) => shot.critical).length} 镜</span></div><div class="audit-critical-list">${criticalRows}</div></section>
      <section class="section-block"><div class="section-head"><h2>分段提示词执行风险</h2><span>${contract.file_count || 0} 个文件 · ${contract.total_unicode_characters || 0} 字符</span></div><table class="audit-prompt-table"><thead><tr><th>段</th><th>任务</th><th>时长</th><th>镜头</th><th>字符</th><th>风险</th></tr></thead><tbody>${promptRows}</tbody></table></section>
      <section class="section-block"><div class="section-head"><h2>判断边界</h2></div><ul class="audit-limitations">${limitations}</ul></section>
    </section>`
  }

  function conciseText(value, maximum = 220) {
    const text = String(value || '').replace(/\s+/g, ' ').trim()
    return text.length > maximum ? `${text.slice(0, maximum).trim()}…` : text
  }

  function feedbackEntryMarkup(event, entry, index) {
    const stage = (event.stages || []).find((item) => item.stage_id === entry.stage_id)
    const scope = stage ? stage.label : '整段'
    return `<div class="feedback-row ${escapeHTML(entry.kind || 'missing')}">
      <span class="feedback-kind">${escapeHTML(feedbackKindLabels[entry.kind] || '人工反馈')}</span>
      <div class="feedback-copy"><b>${escapeHTML(feedbackAxisLabels[entry.axis] || '其他')} · ${escapeHTML(scope)}</b><p>${escapeHTML(entry.text || '')}</p></div>
      <span class="feedback-state">${entry.status === 'applied' ? '已采纳' : '已保存'}</span>
      <button class="remove-feedback" type="button" data-remove-feedback="${index}" data-event-id="${event.event_id}" aria-label="删除这条反馈" title="删除反馈">×</button>
    </div>`
  }

  function renderEvents() {
    const event = selectedEvent()
    if (!event) return '<div class="event-empty"><b>还没有关键事件段</b><p>先把镜头合并为完整因果事件，再比较进入、发展、峰值、反应和离开状态。</p></div>'
    const eventReview = ensureEventReview(event.event_id)
    const analysis = event.analysis || {}
    const aiDraft = analysis.ai_draft && typeof analysis.ai_draft === 'object' ? analysis.ai_draft : analysis
    const humanStatus = eventReview.human_status || 'unreviewed'
    const feedbackEntries = eventReview.feedback_entries || []
    const list = events().map((item) => {
      const itemReview = ensureEventReview(item.event_id)
      const feedbackCount = itemReview.feedback_entries.length
      const shots = (item.source_shot_ids || []).length ? `${item.source_shot_ids[0]}—${item.source_shot_ids[item.source_shot_ids.length - 1]}` : `${item.source.duration_sec.toFixed(2)}s`
      const meta = feedbackCount ? `${feedbackCount} 条反馈 · ${shots}` : shots
      return `<button class="event-button ${item.event_id === event.event_id ? 'on' : ''}" type="button" data-select-event="${item.event_id}"><span class="event-code">${escapeHTML(item.event_id)}</span><span class="event-name"><b>${escapeHTML(item.title)}</b><span>${escapeHTML(meta)}</span></span><i class="pair-state ${escapeHTML(itemReview.human_status || 'unreviewed')}"></i></button>`
    }).join('')
    const invariants = (event.invariants || []).map((item) => `<li>${escapeHTML(item)}</li>`).join('') || '<li>事件顺序、主动者与被动者、力向和结果构图必须可连续验证。</li>'
    const failures = (event.failure_conditions || []).map((item) => `<li>${escapeHTML(item)}</li>`).join('') || '<li>关键动作缺失、顺序颠倒、作用关系错误或桥接状态断裂。</li>'
    const tolerances = (event.tolerances || []).map((item) => `<li>${escapeHTML(item)}</li>`).join('')
    const prompt = event.prompt_trace || {}
    const evidence = event.evidence_sheet ? `<div class="event-evidence"><img src="${mediaURL(event.evidence_sheet)}" alt="${escapeHTML(event.event_id)} 连续事件证据" loading="lazy"></div>` : ''
    const preservedItems = (aiDraft.preserved || []).filter((item) => String(item || '').trim() && String(item).trim() !== String(aiDraft.observed_difference || '').trim()).slice(0, 3)
    const preserved = preservedItems.length ? `<div class="ai-preserved"><b>AI 认为已到位</b>${preservedItems.map((item) => `<p>${escapeHTML(conciseText(item, 150))}</p>`).join('')}</div>` : ''
    const feedbackRows = feedbackEntries.length ? feedbackEntries.map((entry, index) => feedbackEntryMarkup(event, entry, index)).join('') : '<div class="feedback-empty">还没有人工反馈</div>'
    const stageOptions = (event.stages || []).map((stage) => `<option value="${escapeHTML(stage.stage_id)}">${escapeHTML(stage.label)}</option>`).join('')
    const stageStrip = (event.stages || []).map((stage) => {
      const count = feedbackEntries.filter((entry) => entry.stage_id === stage.stage_id).length
      const shotId = String(stage.label || '').split('·')[0].trim() || stage.stage_id
      return `<button class="stage-chip ${count ? 'has-feedback' : ''}" type="button" data-event-window="${event.event_id}" data-stage-id="${stage.stage_id}" title="${escapeHTML(stage.label || '')}"><b>${escapeHTML(shotId)}</b><span>${count ? `${count} 条反馈` : statusLabels[stage.status] || '播放'}</span></button>`
    }).join('')
    const verdictButtons = ['match', 'partial', 'difference', 'uncertain'].map((status) => `<button class="${humanStatus === status ? 'on' : ''}" type="button" data-event-verdict="${status}" data-event-id="${event.event_id}" aria-pressed="${humanStatus === status}">${humanStatusLabels[status]}</button>`).join('')
    const aiStatus = aiDraft.status || 'unreviewed'
    const aiDifference = conciseText(aiDraft.observed_difference || '尚未形成可靠的事件级初判。')
    const aiHistoryLabel = analysis.ai_draft ? '历史草稿 · 已被人工覆盖' : (aiDraft.conclusion_level || '待人工复核')
    const finalizedResult = analysis.ai_draft ? `<div class="human-final-result"><b>已采纳的终审结论</b><p>${escapeHTML(analysis.observed_difference || eventReview.conclusion || '')}</p>${analysis.next_action ? `<span><strong>下一版</strong>${escapeHTML(analysis.next_action)}</span>` : ''}</div>` : ''
    return `<div class="event-layout">
      <aside class="event-rail"><div class="rail-head"><b>逐段审核</b><span>${events().length} 段</span></div><div class="event-list">${list}</div></aside>
      <section class="event-content">
        <div class="event-head"><div><h1>${escapeHTML(event.event_id)} · ${escapeHTML(event.title)}</h1><p>原片 ${event.source.source_in}—${event.source.source_out} · 复刻 ${event.remake.source_in}—${event.remake.source_out}</p></div><div class="event-actions"><button class="quiet-button" type="button" data-event-step="-1">上一段</button><button class="quiet-button" type="button" data-event-step="1">下一段</button><button class="primary-button" type="button" data-event-full="${event.event_id}">播放整段</button></div></div>
        <div class="event-stage-strip" aria-label="段内镜头窗口">${stageStrip}</div>
        <section class="review-workbench">
          <div class="human-review-pane">
            <div class="review-pane-head"><div><h2>你的审核</h2><span>${feedbackEntries.length} 条反馈</span></div><strong class="human-verdict ${escapeHTML(humanStatus)}">${escapeHTML(humanStatusLabels[humanStatus])}</strong></div>
            <div class="verdict-switch" role="group" aria-label="本段复刻结果">${verdictButtons}</div>
            <label class="human-summary-label" for="humanSummary">一句话结论</label>
            <input class="human-summary" id="humanSummary" data-event-field="human_summary" data-event-id="${event.event_id}" value="${escapeHTML(eventReview.human_summary || '')}" placeholder="只写决定是否修改的核心结论">
            ${finalizedResult}
            <div class="feedback-list">${feedbackRows}</div>
            <div class="feedback-composer">
              <select id="feedbackKind" aria-label="反馈类型"><option value="missing">AI 漏识别</option><option value="incorrect">AI 写错</option><option value="confirmed">确认到位</option></select>
              <select id="feedbackAxis" aria-label="反馈关注点"><option value="camera_geometry">镜头形态</option><option value="action_causality">动作因果</option><option value="composition_space">构图空间</option><option value="timing_edit">节奏剪辑</option><option value="vfx_interaction">特效交互</option><option value="character_continuity">角色连续性</option><option value="other">其他</option></select>
              <select id="feedbackStage" aria-label="反馈对应范围"><option value="">整段</option>${stageOptions}</select>
              <textarea id="feedbackText" aria-label="人工反馈内容" placeholder="例如：原片有低机位倾斜和广角透视拉伸，复刻画面过平"></textarea>
              <button class="primary-button" type="button" data-add-event-feedback="${event.event_id}">添加反馈</button>
            </div>
            <div class="review-next"><button class="quiet-button" type="button" data-save-next="${event.event_id}">保存并下一段</button></div>
          </div>
          <aside class="ai-brief">
            <div class="review-pane-head"><div><h2>AI 初判</h2><span>${escapeHTML(aiHistoryLabel)}</span></div><strong class="ai-status ${escapeHTML(aiStatus)}">${escapeHTML(statusLabels[aiStatus] || '待审核')}</strong></div>
            <div class="ai-primary"><b>AI 认为有问题</b><p>${escapeHTML(aiDifference)}</p></div>
            ${preserved}
            <details class="ai-full"><summary>查看原始 AI 分析</summary><dl><dt>完整判断</dt><dd>${escapeHTML(aiDraft.observed_difference || '未填写')}</dd><dt>影响</dt><dd>${escapeHTML(aiDraft.impact || '未填写')}</dd><dt>根因猜测</dt><dd>${escapeHTML(aiDraft.likely_root_cause || '未填写')}</dd><dt>建议</dt><dd>${escapeHTML(aiDraft.next_action || '未填写')}</dd></dl></details>
          </aside>
        </section>
        <details class="deep-review">
          <summary><span>逐镜证据与高级结论</span><small>${(event.stages || []).length} 个镜头窗口</small></summary>
          <div class="event-policy"><b>比较单位：</b>${escapeHTML(comparison.event_segments?.statement || '帧只用于定位证据；验收对象是完整因果事件。')}</div>
          <section class="section-block"><div class="section-head"><h2>逐镜 AI 草稿</h2></div><div class="event-stage-list">${(event.stages || []).map((stage) => eventStageMarkup(event, stage)).join('')}</div></section>
          <div class="event-criteria"><div class="criteria-block"><h2>必须保持</h2><ul>${invariants}</ul></div><div class="criteria-block"><h2>直接判失败</h2><ul>${failures}</ul></div></div>
          <section class="section-block"><div class="section-head"><h2>结构化修改</h2><span>仅在需要形成正式修复任务时填写</span></div><div class="event-review-grid"><div class="field"><label>旧事件判断</label><select data-event-field="status" data-event-id="${event.event_id}">${reviewOptions(eventReview.status || 'unreviewed')}</select></div><div class="field"><label>优先级</label><select data-event-field="severity" data-event-id="${event.event_id}"><option value="high" ${eventReview.severity === 'high' ? 'selected' : ''}>高优先</option><option value="medium" ${eventReview.severity === 'medium' ? 'selected' : ''}>中优先</option><option value="low" ${eventReview.severity === 'low' ? 'selected' : ''}>低优先</option><option value="none" ${eventReview.severity === 'none' ? 'selected' : ''}>接近</option></select></div><div class="field"><label>正式结论</label><input data-event-field="conclusion" data-event-id="${event.event_id}" value="${escapeHTML(eventReview.conclusion || '')}" placeholder="用于修改清单的正式结论"></div>${eventReviewField(event.event_id, 'observed_difference', '可观察差异', eventReview.observed_difference, '真实画面哪里不同')}${eventReviewField(event.event_id, 'impact', '叙事影响', eventReview.impact, '只保留会影响判断的结果')}${eventReviewField(event.event_id, 'root_cause', '根因', eventReview.root_cause, '合并重复症状')}${eventReviewField(event.event_id, 'next_action', '下一版动作', eventReview.next_action, '明确重做范围和验收点')}</div></section>
          <details class="prompt-trace"><summary>提示词追溯与容许误差</summary><div class="prompt-trace-body"><b>容许误差</b><div><ul>${tolerances || '<li>不影响因果、力向和结果构图的次级运动可浮动。</li>'}</ul></div><b>提示词文件</b><p>${escapeHTML(prompt.file || '未关联')}</p><b>原约束</b><p>${escapeHTML(prompt.instruction || '未关联')}</p><b>根因诊断</b><p>${escapeHTML(prompt.diagnosis || analysis.likely_root_cause || '待复核')}</p><b>建议动作</b><p>${escapeHTML(analysis.next_action || '待复核')}</p></div>${evidence}</details>
        </details>
      </section>
    </div>`
  }

  function renderShots() {
    const pair = selectedPair()
    if (!pair) return '<div class="empty-state"><b>还没有镜头对应</b><span>复刻片分析完成后会在这里出现。</span></div>'
    const findingMap = new Map((comparison.findings || []).map((finding) => [finding.finding_id, finding]))
    const findings = (pair.finding_ids || []).map((id) => findingMap.get(id)).filter(Boolean)
    const finding = findings[0]
    const pairReview = ensurePairReview(pair.pair_id)
    const rail = pairs().map((item, index) => {
      const severity = pairSeverity(item)
      const duration = item.status === 'matched' ? `${item.source.duration_sec.toFixed(2)}s / ${item.remake.duration_sec.toFixed(2)}s` : item.status === 'missing' ? '复刻缺失' : '复刻新增'
      return `<button class="pair-button ${item.pair_id === pair.pair_id ? 'on' : ''}" type="button" data-select-pair="${item.pair_id}"><span class="pair-index">${String(index + 1).padStart(2, '0')}</span><span class="pair-names"><b>${escapeHTML(pairLabel(item))}</b><span>${escapeHTML(duration)}</span></span><i class="pair-state ${severity}"></i></button>`
    }).join('')
    const findingBand = finding
      ? `<div class="finding-band"><div><b>${escapeHTML(finding.dimension)} · ${escapeHTML(finding.visible_difference)}</b><p>${escapeHTML(finding.impact)} ${escapeHTML(finding.recommendation)}</p></div><span class="severity ${finding.severity}">${severityLabels[finding.severity] || finding.severity}</span></div>`
      : '<div class="finding-band none"><div><b>机械数据未发现明显时长或缺镜问题</b><p>构图、动作、摄影机、特效和连续性仍需结合连续画面审核。</p></div></div>'
    const sourceName = pair.source?.shot_id || '缺失'
    const remakeName = pair.remake?.shot_id || '缺失'
    return `<div class="review-layout">
      <aside class="pair-rail"><div class="rail-head"><b>镜头对应</b><span>${pairs().length} 组</span></div><div class="pair-list">${rail}</div></aside>
      <section class="pair-review">
        <div class="pair-review-head"><div class="pair-review-title"><h1>${pair.pair_id} · ${escapeHTML(sourceName)} 对 ${escapeHTML(remakeName)}</h1><p>${pair.status === 'matched' ? `原片 ${pair.source.source_in}—${pair.source.source_out} · 复刻 ${pair.remake.source_in}—${pair.remake.source_out}` : pair.status === 'missing' ? '原片镜头在复刻片中没有对应项。' : '复刻片出现原片没有的独立镜头。'}</p></div><div class="pair-nav"><button class="quiet-button" type="button" data-pair-step="-1">上一组</button><button class="quiet-button" type="button" data-pair-step="1">下一组</button></div></div>
        ${findingBand}
        <section class="section-block"><div class="section-head"><h2>关键节点对照</h2><span>开场、中段、候选峰值与收势桥接</span></div><div class="node-list">${(pair.nodes || []).map((node) => nodeMarkup(pair, node)).join('')}</div></section>
        <section class="section-block"><div class="section-head"><h2>七维审核</h2><span>术语不参与自动评分</span></div><div class="dimension-list">${(comparison.dimensions || []).map((dimension) => dimensionMarkup(pair, dimension)).join('')}</div></section>
        <section class="section-block"><div class="section-head"><h2>本组结论</h2><span>合并根因，不重复罗列症状</span></div><textarea class="overall-note" data-pair-conclusion="${pair.pair_id}" placeholder="这一组最根本的偏差是什么？它同时造成了哪些节奏、动作、构图或特效问题？">${escapeHTML(pairReview.conclusion || '')}</textarea></section>
      </section>
    </div>`
  }

  function timelineShot(pair, side, total) {
    const shot = pair[side]
    if (!shot) return ''
    const severity = pairSeverity(pair)
    const width = Math.max(.35, Number(shot.duration_sec || 0) / Math.max(.001, total) * 100)
    return `<button class="timeline-shot ${severity}" style="width:${width}%" type="button" data-select-pair="${pair.pair_id}" title="${escapeHTML(shot.shot_id)} · ${shot.duration_sec.toFixed(3)} 秒"><span>${escapeHTML(shot.shot_id)}</span></button>`
  }

  function renderRhythm() {
    const summary = comparison.summary || {}
    const sourceTotal = Number(summary.reference_duration_sec || 1)
    const remakeTotal = Number(summary.remake_duration_sec || 1)
    const rows = pairs().map((pair) => {
      const metrics = pair.metrics || {}
      return `<tr data-pair-id="${pair.pair_id}"><td><button class="quiet-button" type="button" data-select-pair="${pair.pair_id}">${pair.pair_id}</button></td><td>${escapeHTML(pair.source?.shot_id || '—')}</td><td>${escapeHTML(pair.remake?.shot_id || '—')}</td><td>${pair.source ? `${pair.source.duration_sec.toFixed(3)}s` : '—'}</td><td>${pair.remake ? `${pair.remake.duration_sec.toFixed(3)}s` : '—'}</td><td>${pair.status === 'matched' ? signedSeconds(metrics.duration_delta_sec) : pair.status === 'missing' ? '缺镜' : '增镜'}</td><td>${pair.status === 'matched' ? signedSeconds(metrics.cut_out_drift_sec) : '—'}</td></tr>`
    }).join('')
    return `<section class="wide-view">
      <div class="wide-head"><div><h1>全片节奏与切点</h1><p>上下时间线各自按真实时长展开。这里比较镜头数量、长度和切点漂移，不把角色造型差异转换成像素质量分。</p></div><button class="quiet-button" id="openAlignmentFromRhythm" type="button">修正对应</button></div>
      <div class="timeline"><div class="timeline-row"><span class="timeline-label">原片</span><div class="timeline-track">${pairs().map((pair) => timelineShot(pair, 'source', sourceTotal)).join('')}</div></div><div class="timeline-row"><span class="timeline-label">复刻片</span><div class="timeline-track">${pairs().map((pair) => timelineShot(pair, 'remake', remakeTotal)).join('')}</div></div></div>
      <table class="metrics-table"><thead><tr><th>对应</th><th>原片镜头</th><th>复刻镜头</th><th>原片时长</th><th>复刻时长</th><th>时长差</th><th>归一切点漂移</th></tr></thead><tbody>${rows}</tbody></table>
    </section>`
  }

  function queueId(prefix = 'Q') {
    return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`
  }

  function findingInQueue(findingId) {
    return ensureReview().fix_queue.some((item) => item.source_finding_id === findingId || item.id === `Q-${findingId}`)
  }

  function renderQueue() {
    const findings = comparison.findings || []
    const queue = ensureReview().fix_queue
    const feedback = events().flatMap((event) => ensureEventReview(event.event_id).feedback_entries.map((entry) => ({ ...entry, event })))
    const findingRows = findings.length ? findings.map((finding) => `<div class="finding-row"><strong>${severityLabels[finding.severity] || finding.severity}</strong><div class="finding-copy"><b>${escapeHTML(finding.pair_id)} · ${escapeHTML(finding.visible_difference)}</b><p>${escapeHTML(finding.recommendation)}</p></div><button class="quiet-button" type="button" data-add-finding="${finding.finding_id}" ${findingInQueue(finding.finding_id) ? 'disabled' : ''}>${findingInQueue(finding.finding_id) ? '已加入' : '加入队列'}</button></div>`).join('') : '<div class="empty-state"><b>没有机械差异条目</b><span>人工审核中发现的问题仍可直接加入右侧队列。</span></div>'
    const feedbackRows = feedback.length ? feedback.map((entry) => {
      const stage = (entry.event.stages || []).find((item) => item.stage_id === entry.stage_id)
      return `<div class="queue-feedback ${escapeHTML(entry.kind)}"><span>${escapeHTML(feedbackKindLabels[entry.kind] || '人工反馈')}</span><div><b>${escapeHTML(feedbackAxisLabels[entry.axis] || '其他')} · ${escapeHTML(entry.event.event_id)} · ${escapeHTML(stage?.label || entry.event.title)}</b><p>${escapeHTML(entry.text)}</p></div><button class="quiet-button" type="button" data-open-event-review="${entry.event.event_id}">查看</button></div>`
    }).join('') : '<div class="empty-state compact"><b>还没有人工反馈</b><span>逐段审核中添加的内容会汇总在这里。</span></div>'
    const queueRows = queue.length ? queue.map((item, index) => `<div class="queue-item"><select data-queue-field="status" data-queue-index="${index}"><option value="open" ${item.status === 'open' ? 'selected' : ''}>待处理</option><option value="doing" ${item.status === 'doing' ? 'selected' : ''}>处理中</option><option value="done" ${item.status === 'done' ? 'selected' : ''}>已完成</option></select><div class="queue-copy"><b>${escapeHTML(item.event_id || item.pair_id || '全片')} · ${escapeHTML(item.problem || '未命名问题')}</b><p>${escapeHTML(item.action || '')}</p></div><button class="remove-item" type="button" data-remove-queue="${index}" aria-label="移除修复项">×</button></div>`).join('') : '<div class="empty-state"><b>修复队列还是空的</b><span>只加入能指导下一版制作的根因，不重复堆叠同一问题的症状。</span></div>'
    return `<section class="wide-view"><div class="wide-head"><div><h1>人工反馈与修改清单</h1><p>人工结论优先；机器候选仅在需要补充证据时展开。</p></div></div><div class="queue-layout"><div><div class="section-head"><h2>人工反馈</h2><span>${feedback.length} 条</span></div><div class="queue-feedback-list">${feedbackRows}</div><div class="section-head" style="margin-top:24px"><h2>已确认修复项</h2><span>${queue.length} 条</span></div><div class="queue-items">${queueRows}</div><details class="machine-candidates"><summary>机器候选 · ${findings.length} 条</summary><div class="auto-findings">${findingRows}</div></details><div class="section-block"><div class="section-head"><h2>全片复核备注</h2></div><textarea class="overall-note" id="overallReviewNote" placeholder="记录跨镜的共同根因、优先级或下一轮验收顺序…">${escapeHTML(ensureReview().overall_note)}</textarea></div></div><aside class="queue-editor"><h2>新增修复项</h2><div class="queue-field"><label for="queueTarget">对应范围</label><select id="queueTarget"><option value="">全片</option>${events().map((event) => `<option value="event:${event.event_id}">${event.event_id} · ${escapeHTML(event.title)}</option>`).join('')}${pairs().map((pair) => `<option value="pair:${pair.pair_id}">${pair.pair_id} · ${escapeHTML(pairLabel(pair))}</option>`).join('')}</select></div><div class="queue-field"><label for="queueSeverity">优先级</label><select id="queueSeverity"><option value="high">高</option><option value="medium" selected>中</option><option value="low">低</option></select></div><div class="queue-field"><label for="queueProblem">根本问题</label><input id="queueProblem" placeholder="例如：重击峰值整体晚于原片"></div><div class="queue-field"><label for="queueAction">下一版修改动作</label><textarea id="queueAction" placeholder="写清要改哪个时间段、动作阶段、机位或特效层…"></textarea></div><div class="queue-actions"><button class="primary-button" id="addQueueItem" type="button">加入队列</button></div></aside></div></section>`
  }

  function renderTech() {
    const items = ensureReview().technical_identifications
    const list = items.length ? items.map((item, index) => `<div class="tech-item"><b>${escapeHTML(item.term || '未命名技术')}</b><span class="evidence-level">${escapeHTML(item.evidence_level || '无法确认')}</span><p>${escapeHTML(item.event_id || item.pair_id ? `${item.event_id || item.pair_id} · ${item.basis || ''}` : item.basis || '')}</p><button class="remove-item" type="button" data-remove-tech="${index}" aria-label="移除技术鉴定">×</button></div>`).join('') : '<div class="empty-state"><b>还没有技术鉴定记录</b><span>只有证据等级和区分依据完整的术语才值得沉淀。</span></div>'
    return `<section class="wide-view"><div class="wide-head"><div><h1>电影技术鉴定</h1><p>这条辅轨用于学习和专业校对，不参与主流程评分，也不会覆盖可观察差异。</p></div></div><div class="tech-boundary"><b>证据等级：</b> 已观察 → 与某技术特征匹配 → 有工程数据验证；素材不足时保留“无法确认”。</div><div class="tech-form"><div class="tech-field"><label for="techTerm">技术名称</label><input id="techTerm" placeholder="如：低机位固定镜头"></div><div class="tech-field"><label for="techLevel">证据等级</label><select id="techLevel"><option>已观察</option><option>与某技术特征匹配</option><option>有工程数据验证</option><option selected>无法确认</option></select></div><div class="tech-field"><label for="techBasis">区分依据</label><textarea id="techBasis" placeholder="写明主体位移、摄影机位移、焦距变化、景深或光线证据…"></textarea></div><button class="primary-button" id="addTechItem" type="button">记录</button></div><div class="tech-list">${list}</div></section>`
  }

  function renderActiveTab() {
    const panel = $('#tabPanel')
    panel.innerHTML = activeTab === 'audit' ? renderAudit() : activeTab === 'events' ? renderEvents() : activeTab === 'shots' ? renderShots() : activeTab === 'rhythm' ? renderRhythm() : activeTab === 'queue' ? renderQueue() : renderTech()
  }

  function renderAll() {
    ensureReview()
    statusBanner()
    renderSummary()
    initializeVideos()
    const hasAudit = Boolean(auditBundle()?.review_status)
    const hasEvents = events().length > 0
    $('#auditTab').hidden = !hasAudit
    $('#eventsTab').hidden = !hasEvents
    if (!hasAudit && activeTab === 'audit') activeTab = hasEvents ? 'events' : 'shots'
    if (!hasEvents && activeTab === 'events') activeTab = 'shots'
    const hashId = decodeURIComponent(location.hash.replace(/^#/, ''))
    if (!events().some((event) => event.event_id === selectedEventId)) selectedEventId = events().some((event) => event.event_id === hashId) ? hashId : events()[0]?.event_id || ''
    if (!pairs().some((pair) => pair.pair_id === selectedPairId)) selectedPairId = pairs().some((pair) => pair.pair_id === hashId) ? hashId : pairs()[0]?.pair_id || ''
    if (monitorSelection.type === 'event' && !events().some((event) => event.event_id === monitorSelection.id)) monitorSelection = { type: hasEvents ? 'event' : 'pair', id: hasEvents ? selectedEventId : selectedPairId }
    if (monitorSelection.type === 'pair' && !pairs().some((pair) => pair.pair_id === monitorSelection.id)) monitorSelection = { type: hasEvents && activeTab === 'events' ? 'event' : 'pair', id: hasEvents && activeTab === 'events' ? selectedEventId : selectedPairId }
    $$('.tabs button').forEach((button) => button.classList.toggle('on', button.dataset.tab === activeTab))
    renderMonitorSelection()
    renderActiveTab()
  }

  async function loadComparison() {
    try {
      const response = await fetch(apiBase(), { cache: 'no-store' })
      const data = await response.json().catch(() => ({}))
      if (!response.ok || !data.comparison) throw new Error(data.error || `读取失败：HTTP ${response.status}`)
      comparison = data.comparison
      if (lastStatus && lastStatus !== 'ready' && comparison.status === 'ready') {
        const dropped = Number(comparison.review_remap?.dropped_pair_reviews || 0)
        toast(dropped ? `镜头对应已更新；${dropped} 组旧审核因对应变化未沿用` : '镜头对应与证据已更新')
      }
      lastStatus = comparison.status
      renderAll()
      if (comparison.status !== 'ready' && comparison.status !== 'failed') startPolling()
      else stopPolling()
    } catch (error) {
      $('#statusBanner').classList.add('visible')
      $('#statusStage').textContent = '无法读取最新对比状态'
      $('#statusDetail').textContent = error.message || '本地连接失败'
    }
  }

  function startPolling() {
    if (pollTimer) return
    pollTimer = setInterval(loadComparison, 900)
  }

  function stopPolling() {
    clearInterval(pollTimer)
    pollTimer = null
  }

  function openAlignment() {
    const sourceShots = pairs().map((pair) => pair.source).filter(Boolean).sort((a, b) => a.index - b.index)
    const remakeShots = pairs().map((pair) => pair.remake).filter(Boolean).sort((a, b) => a.index - b.index)
    const uniqueRemakes = [...new Map(remakeShots.map((shot) => [shot.shot_id, shot])).values()]
    const mapping = new Map(pairs().filter((pair) => pair.source).map((pair) => [pair.source.shot_id, pair.remake?.shot_id || '']))
    $('#alignmentEditor').innerHTML = `<table class="alignment-table"><thead><tr><th>原片镜头</th><th>时段</th><th>复刻对应</th></tr></thead><tbody>${sourceShots.map((shot) => `<tr><td><b>${escapeHTML(shot.shot_id)}</b></td><td>${escapeHTML(shot.source_in)}—${escapeHTML(shot.source_out)}</td><td><select data-align-source="${shot.shot_id}"><option value="">缺失</option>${uniqueRemakes.map((remake) => `<option value="${remake.shot_id}" ${mapping.get(shot.shot_id) === remake.shot_id ? 'selected' : ''}>${escapeHTML(remake.shot_id)} · ${escapeHTML(remake.source_in)}—${escapeHTML(remake.source_out)}</option>`).join('')}</select></td></tr>`).join('')}</tbody></table>`
    $('#alignmentError').hidden = true
    $('#alignmentDialog').showModal()
  }

  async function applyAlignment() {
    if (reviewDirty) await saveReview(false)
    const selects = $$('[data-align-source]', $('#alignmentEditor'))
    const selectedIndexes = selects.filter((select) => select.value).map((select) => select.selectedIndex)
    if (new Set(selectedIndexes).size !== selectedIndexes.length || selectedIndexes.some((value, index) => index > 0 && value <= selectedIndexes[index - 1])) {
      $('#alignmentError').textContent = '复刻镜头不能重复使用，也不能打乱原有时间顺序。'
      $('#alignmentError').hidden = false
      return
    }
    const alignment = selects.map((select) => ({ source_shot_id: select.dataset.alignSource, remake_shot_id: select.value || null }))
    const button = $('#applyAlignment')
    button.disabled = true
    button.textContent = '正在应用…'
    $('#alignmentError').hidden = true
    try {
      const response = await fetch(`${apiBase()}/alignment`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ alignment }) })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(data.error || `应用失败：HTTP ${response.status}`)
      comparison = data.comparison || comparison
      $('#alignmentDialog').close()
      renderAll()
      startPolling()
      toast('正在按人工对应关系重建证据')
    } catch (error) {
      $('#alignmentError').textContent = error.message || '无法应用镜头对应'
      $('#alignmentError').hidden = false
    } finally {
      button.disabled = false
      button.textContent = '应用并重建证据'
    }
  }

  function updateNodeReview(target) {
    const pair = selectedPair()
    if (!pair) return
    const nodeId = target.dataset.nodeId
    const field = target.dataset.nodeField
    const pairReview = ensurePairReview(pair.pair_id)
    if (!pairReview.node_reviews[nodeId]) pairReview.node_reviews[nodeId] = { status: 'unreviewed', event_label: '', note: '' }
    pairReview.node_reviews[nodeId][field] = target.value
    markDirty()
  }

  function updateEventReview(target) {
    const eventId = target.dataset.eventId
    const field = target.dataset.eventField
    if (!eventId || !field) return
    ensureEventReview(eventId)[field] = target.value
    markDirty()
  }

  function setEventVerdict(eventId, status) {
    if (!['match', 'partial', 'difference', 'uncertain'].includes(status)) return
    const review = ensureEventReview(eventId)
    review.human_status = status
    review.status = status
    review.severity = status === 'match' ? 'none' : status === 'difference' ? 'high' : 'medium'
    markDirty()
    renderSummary()
    renderActiveTab()
  }

  function addEventFeedback(eventId) {
    const text = $('#feedbackText')?.value.trim()
    if (!text) { toast('请先写下要补充或纠正的事实'); return }
    const review = ensureEventReview(eventId)
    review.feedback_entries.push({
      id: queueId('FB'),
      kind: $('#feedbackKind')?.value || 'missing',
      axis: $('#feedbackAxis')?.value || 'other',
      stage_id: $('#feedbackStage')?.value || '',
      text,
      status: 'open',
      created_at: new Date().toISOString(),
    })
    markDirty()
    renderSummary()
    renderActiveTab()
    toast('人工反馈已加入')
  }

  function removeEventFeedback(eventId, index) {
    const entries = ensureEventReview(eventId).feedback_entries
    if (!entries[index]) return
    entries.splice(index, 1)
    markDirty()
    renderSummary()
    renderActiveTab()
  }

  async function saveAndSelectNext() {
    const saved = await saveReview(true)
    if (saved) selectAdjacent(1)
  }

  function updateEventStageReview(target) {
    const eventId = target.dataset.eventId
    const stageId = target.dataset.stageId
    const field = target.dataset.eventStageField
    if (!eventId || !stageId || !field) return
    const review = ensureEventReview(eventId)
    if (!review.stage_reviews[stageId]) review.stage_reviews[stageId] = { status: 'unreviewed', note: '' }
    review.stage_reviews[stageId][field] = target.value
    markDirty()
  }

  function setEventWindow(eventId, stageId = '', { autoplay = true } = {}) {
    const event = events().find((item) => item.event_id === eventId)
    if (!event) return
    selectedEventId = eventId
    monitorSelection = { type: 'event', id: eventId }
    const stage = stageId ? (event.stages || []).find((item) => item.stage_id === stageId) : null
    monitorWindow = stage ? { source: stage.source, remake: stage.remake, label: `${stage.label} · ${stage.source.source_in}—${stage.source.source_out}` } : null
    renderMonitorSelection()
    $('#monitor').scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' })
    if (autoplay) void togglePlayback()
  }

  function setAuditShotWindow(shotId) {
    const shot = (auditBundle()?.shots || []).find((item) => item.shot_id === shotId)
    const event = events().find((item) => item.event_id === shot?.segment_id)
    if (!shot || !event) return
    selectedEventId = event.event_id
    monitorSelection = { type: 'event', id: event.event_id }
    monitorWindow = {
      source: { start_sec: shot.source_start_sec, end_sec: shot.source_end_sec, duration_sec: Math.max(.001, shot.source_end_sec - shot.source_start_sec) },
      remake: { start_sec: shot.candidate_start_sec, end_sec: shot.candidate_end_sec, duration_sec: Math.max(.001, shot.candidate_end_sec - shot.candidate_start_sec) },
      label: `${shot.shot_id} · ${shot.alignment_basis === 'manual_dense_review' ? '人工密集帧范围' : '段内插值定位'}`,
    }
    renderMonitorSelection()
    $('#monitor').scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' })
  }

  function updateDimensionReview(target) {
    const pair = selectedPair()
    if (!pair) return
    const dimension = target.dataset.dimension
    const field = target.dataset.dimensionField
    const pairReview = ensurePairReview(pair.pair_id)
    if (!pairReview.dimensions[dimension]) pairReview.dimensions[dimension] = { status: 'unreviewed', note: '', evidence: '' }
    pairReview.dimensions[dimension][field] = target.value
    markDirty()
  }

  function addFinding(findingId) {
    const finding = (comparison.findings || []).find((item) => item.finding_id === findingId)
    if (!finding || findingInQueue(findingId)) return
    ensureReview().fix_queue.push({ id: `Q-${findingId}`, source_finding_id: findingId, pair_id: finding.pair_id, severity: finding.severity, status: 'open', problem: finding.visible_difference, action: finding.recommendation })
    markDirty()
    renderActiveTab()
  }

  function addQueueItem() {
    const problem = $('#queueProblem')?.value.trim()
    const action = $('#queueAction')?.value.trim()
    if (!problem || !action) { toast('请填写根本问题和具体修改动作'); return }
    const [targetType, targetId] = String($('#queueTarget')?.value || '').split(':')
    ensureReview().fix_queue.push({ id: queueId(), pair_id: targetType === 'pair' ? targetId : '', event_id: targetType === 'event' ? targetId : '', severity: $('#queueSeverity').value, status: 'open', problem, action })
    markDirty()
    renderActiveTab()
  }

  function addTechItem() {
    const term = $('#techTerm')?.value.trim()
    const basis = $('#techBasis')?.value.trim()
    if (!term || !basis) { toast('请填写技术名称和区分依据'); return }
    ensureReview().technical_identifications.push({ id: queueId('T'), pair_id: monitorSelection.type === 'pair' ? selectedPairId : '', event_id: monitorSelection.type === 'event' ? selectedEventId : '', term, evidence_level: $('#techLevel').value, basis })
    markDirty()
    renderActiveTab()
  }

  function openEventReview(eventId) {
    activeTab = 'events'
    $$('.tabs button').forEach((button) => button.classList.toggle('on', button.dataset.tab === activeTab))
    selectEvent(eventId, { scroll: true })
  }

  $('#tabPanel').addEventListener('click', (event) => {
    const verdict = event.target.closest('[data-event-verdict]')
    if (verdict) { setEventVerdict(verdict.dataset.eventId, verdict.dataset.eventVerdict); return }
    const addFeedback = event.target.closest('[data-add-event-feedback]')
    if (addFeedback) { addEventFeedback(addFeedback.dataset.addEventFeedback); return }
    const removeFeedback = event.target.closest('[data-remove-feedback]')
    if (removeFeedback) { removeEventFeedback(removeFeedback.dataset.eventId, Number(removeFeedback.dataset.removeFeedback)); return }
    const saveNext = event.target.closest('[data-save-next]')
    if (saveNext) { void saveAndSelectNext(); return }
    const openReview = event.target.closest('[data-open-event-review]')
    if (openReview) { openEventReview(openReview.dataset.openEventReview); return }
    const auditFilterButton = event.target.closest('[data-audit-filter]')
    if (auditFilterButton) { auditFilter = auditFilterButton.dataset.auditFilter; renderActiveTab(); return }
    const auditShot = event.target.closest('[data-audit-shot]')
    if (auditShot) { setAuditShotWindow(auditShot.dataset.auditShot); return }
    const eventButton = event.target.closest('[data-select-event]')
    if (eventButton) { selectEvent(eventButton.dataset.selectEvent); return }
    const eventStep = event.target.closest('[data-event-step]')
    if (eventStep) { selectAdjacent(Number(eventStep.dataset.eventStep)); return }
    const eventWindow = event.target.closest('[data-event-window]')
    if (eventWindow) { setEventWindow(eventWindow.dataset.eventWindow, eventWindow.dataset.stageId); return }
    const eventFull = event.target.closest('[data-event-full]')
    if (eventFull) { setEventWindow(eventFull.dataset.eventFull); return }
    const pairButton = event.target.closest('[data-select-pair]')
    if (pairButton) { selectPair(pairButton.dataset.selectPair, { scroll: event.target.closest('.timeline,.metrics-table') != null }); return }
    const step = event.target.closest('[data-pair-step]')
    if (step) { selectAdjacent(Number(step.dataset.pairStep)); return }
    const seek = event.target.closest('[data-seek-phase]')
    if (seek) { seekPhase(Number(seek.dataset.seekPhase)); $('#monitor').scrollIntoView({ behavior: 'smooth', block: 'start' }); return }
    const addFindingButton = event.target.closest('[data-add-finding]')
    if (addFindingButton) { addFinding(addFindingButton.dataset.addFinding); return }
    const removeQueue = event.target.closest('[data-remove-queue]')
    if (removeQueue) { ensureReview().fix_queue.splice(Number(removeQueue.dataset.removeQueue), 1); markDirty(); renderActiveTab(); return }
    const removeTech = event.target.closest('[data-remove-tech]')
    if (removeTech) { ensureReview().technical_identifications.splice(Number(removeTech.dataset.removeTech), 1); markDirty(); renderActiveTab(); return }
    if (event.target.id === 'addQueueItem') addQueueItem()
    if (event.target.id === 'addTechItem') addTechItem()
    if (event.target.id === 'openAlignmentFromRhythm') openAlignment()
  })

  $('#tabPanel').addEventListener('input', (event) => {
    const target = event.target
    if (target.dataset.eventStageField) updateEventStageReview(target)
    else if (target.dataset.eventField) updateEventReview(target)
    else if (target.dataset.nodeField) updateNodeReview(target)
    else if (target.dataset.dimensionField) updateDimensionReview(target)
    else if (target.dataset.pairConclusion) { ensurePairReview(target.dataset.pairConclusion).conclusion = target.value; markDirty() }
    else if (target.id === 'overallReviewNote') { ensureReview().overall_note = target.value; markDirty() }
  })

  $('#tabPanel').addEventListener('change', (event) => {
    const target = event.target
    if (target.dataset.eventStageField) updateEventStageReview(target)
    else if (target.dataset.eventField) updateEventReview(target)
    else if (target.dataset.nodeField) updateNodeReview(target)
    else if (target.dataset.dimensionField) updateDimensionReview(target)
    else if (target.dataset.queueField) { ensureReview().fix_queue[Number(target.dataset.queueIndex)][target.dataset.queueField] = target.value; markDirty() }
  })

  $$('.tabs button').forEach((button) => button.addEventListener('click', () => {
    activeTab = button.dataset.tab
    $$('.tabs button').forEach((item) => item.classList.toggle('on', item === button))
    if (activeTab === 'events' && selectedEvent()) {
      monitorSelection = { type: 'event', id: selectedEvent().event_id }
      monitorWindow = null
      renderMonitorSelection()
    } else if (activeTab === 'audit' && selectedEvent()) {
      monitorSelection = { type: 'event', id: selectedEvent().event_id }
      monitorWindow = null
      renderMonitorSelection()
    } else if (activeTab === 'shots' && selectedPair()) {
      monitorSelection = { type: 'pair', id: selectedPair().pair_id }
      monitorWindow = null
      renderMonitorSelection()
    }
    renderActiveTab()
  }))

  $$('[data-view-mode]').forEach((button) => button.addEventListener('click', () => {
    const mode = button.dataset.viewMode
    $('#players').dataset.mode = mode
    $$('[data-view-mode]').forEach((item) => item.classList.toggle('on', item === button))
    $('#abSwitch').style.display = mode === 'ab' ? '' : 'none'
  }))

  $('#abSwitch').addEventListener('click', () => {
    abSide = abSide === 'source' ? 'remake' : 'source'
    $('#players').dataset.ab = abSide
    $('#abSwitch').textContent = abSide === 'source' ? '显示 A' : '显示 B'
    $('#abSwitch').setAttribute('aria-pressed', String(abSide === 'remake'))
  })
  $('#abSwitch').style.display = 'none'
  $('#overlayOpacity').addEventListener('input', (event) => $('#players').style.setProperty('--overlay-opacity', String(Number(event.target.value) / 100)))
  $('#playButton').addEventListener('click', togglePlayback)
  $('#restartButton').addEventListener('click', () => seekPhase(0))
  $('#phaseSlider').addEventListener('input', (event) => seekPhase(Number(event.target.value) / 1000))
  $('#syncMode').addEventListener('change', () => { applyPlaybackRates(); seekPhase(currentPhase) })
  $('#speedSelect').addEventListener('change', applyPlaybackRates)
  $('#audioMode').addEventListener('change', updateAudioMode)
  $('#sourceVideo').addEventListener('timeupdate', syncFromMaster)
  $('#remakeVideo').addEventListener('timeupdate', syncFromMaster)
  $('#sourceVideo').addEventListener('click', togglePlayback)
  $('#remakeVideo').addEventListener('click', togglePlayback)
  $('#saveReviewButton').addEventListener('click', () => saveReview(true))
  $('#alignmentButton').addEventListener('click', openAlignment)
  $('#closeAlignment').addEventListener('click', () => $('#alignmentDialog').close())
  $('#cancelAlignment').addEventListener('click', () => $('#alignmentDialog').close())
  $('#applyAlignment').addEventListener('click', applyAlignment)

  document.addEventListener('keydown', (event) => {
    const tag = event.target.tagName
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag) || event.metaKey || event.ctrlKey || event.altKey) return
    if (event.key.toLowerCase() === 'j') { event.preventDefault(); selectAdjacent(1) }
    if (event.key.toLowerCase() === 'k') { event.preventDefault(); selectAdjacent(-1) }
    if (event.key === ' ') { event.preventDefault(); togglePlayback() }
  })

  window.addEventListener('beforeunload', () => {
    if (!reviewDirty) return
    fetch(`${apiBase()}/review`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ review: ensureReview() }), keepalive: true }).catch(() => {})
  })

  renderAll()
  loadComparison()
})()
