(() => {
  'use strict'

  const config = window.LAPIAN_CONFIG || {}
  const PROJECT = window.LAPIAN_PROJECT || {}
  const storageScope = String(PROJECT.storage_scope || PROJECT.project_id || '').replace(/[^\w.-]+/g, '-').slice(0, 120)
  const scopedStorageKey = (key) => storageScope ? `${key}:${storageScope}` : key
  const STORAGE_KEY = scopedStorageKey(config.storageKey || 'lapian-chaoren-review-state-v2')
  const SETTINGS_KEY = config.settingsKey || 'lapian-chaoren-settings-v2'
  const ADVISOR_STORAGE_KEY = scopedStorageKey(config.advisorStorageKey || 'lapian-chaoren-ai-advisor-v1')
  const ADVISOR_ENDPOINT = config.aiEndpoint || 'http://127.0.0.1:8766/api/advisor'
  const INITIAL_ANALYSIS_ENDPOINT = config.aiInitialEndpoint || ADVISOR_ENDPOINT.replace(/\/api\/advisor\/?$/, '/api/analyze-shot')
  const ADVISOR_HEALTH_ENDPOINT = config.aiHealthEndpoint || ADVISOR_ENDPOINT.replace(/\/api\/advisor\/?$/, '/api/health')
  const APP_NAME = config.appName || '拉片超人'
  const FIELD_LABELS = {
    previous_shot_result: '上一镜结果', boundary_evidence: '切点证据', opening_frame_literal: '第一帧', screen_layout: '屏幕布局',
    literal_visual_description: '整镜真实画面', character_positions: '人物位置', clone_count_and_positions: '分身', body_part_specifics: '具体肢体',
    timeline_beats: '动作分拍', force_contact_response: '力 / 接触 / 响应', performance_intent: '表演', camera_position: '机位', camera_motion: '运镜',
    lens_fov: '焦段 / FOV', focus_depth: '焦点 / 景深', composition_editing: '构图 / 剪辑', vfx_layers_timing: 'VFX 层与时序',
    interactive_lighting: '交互光', environment_response: '环境响应', sound_beats: '声音 / 剪辑节拍', end_state: '结束状态', next_shot_bridge: '下一镜桥接',
    often_missed: '最容易漏写', uncertainties: '不确定项', rebuild_route: '建议重制路线', source_fidelity_vs_adaptation: '原片忠实度与目标适配',
  }
  const EDITABLE_FIELDS = ['title', 'analysis_confidence', ...Object.keys(FIELD_LABELS)]
  const LEARNING_FIELDS = [
    'literal_visual_description', 'timeline_beats', 'body_part_specifics', 'force_contact_response',
    'camera_motion', 'focus_depth', 'vfx_layers_timing', 'next_shot_bridge',
  ]
  const CORRECTION_REASONS = [
    ['', '选择纠错原因'],
    ['accepted_with_evidence', 'AI 判断与证据一致'],
    ['missed_detail', '遗漏可见细节'],
    ['camera_subject_confusion', '摄影机与主体位移混淆'],
    ['focus_lens_confusion', '焦点、变焦或焦段混淆'],
    ['action_too_vague', '动作与肢体不具体'],
    ['force_chain_wrong', '接触或受力链错误'],
    ['vfx_timing_missing', 'VFX 时序缺失'],
    ['continuity_broken', '前后镜连续性错误'],
    ['evidence_insufficient', '现有证据不足'],
    ['other', '其他'],
  ]
  const REPLICATION_CRITERIA = [
    ['action', '动作与力学'], ['camera', '摄影机'], ['vfx', 'VFX 与交互光'],
    ['identity', '角色身份'], ['continuity', '镜间连续性'],
  ]
  const GROUPS = [
    ['连续性', ['previous_shot_result', 'boundary_evidence', 'opening_frame_literal', 'end_state', 'next_shot_bridge']],
    ['画面与角色', ['screen_layout', 'literal_visual_description', 'character_positions', 'clone_count_and_positions', 'body_part_specifics']],
    ['动作与表演', ['timeline_beats', 'force_contact_response', 'performance_intent']],
    ['摄影与剪辑', ['camera_position', 'camera_motion', 'lens_fov', 'focus_depth', 'composition_editing']],
    ['特效、环境与声音', ['vfx_layers_timing', 'interactive_lighting', 'environment_response', 'sound_beats']],
    ['风险与重制', ['often_missed', 'uncertainties', 'rebuild_route', 'source_fidelity_vs_adaptation']],
  ]
  const CHECKS = ['角色身份、方位与主体数量明确', '动作前因、过程和结果完整', '摄影机运动与角色位移已区分', 'VFX 发生时机和空间层级已定位', '前后镜姿态、力向和能量尾迹连续']
  const ADVISOR_MODES = [
    ['open', '自由提问'], ['terminology', '术语辨认'], ['camera', '摄影机'], ['lighting', '灯光'],
    ['color', '色调'], ['grading', '调色'], ['post', '后期质感'], ['rebuild', '复现方案'],
  ]
  const ADVISOR_QUICK_QUESTIONS = [
    ['camera', '这个运镜专业上叫什么？'], ['lighting', '这段光是怎么布的？'], ['color', '这个色调如何准确描述？'],
    ['grading', '后期调色可能做了什么？'], ['rebuild', '如果重制，应该怎么搭？'],
  ]
  const $ = (selector) => document.querySelector(selector)
  const $$ = (selector) => [...document.querySelectorAll(selector)]
  const escapeHTML = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[char])
  const truncate = (value, length = 112) => { const text = String(value ?? ''); return text.length > length ? `${text.slice(0, length)}…` : text }
  const readJSON = (key, fallback) => { try { return JSON.parse(localStorage.getItem(key)) || fallback } catch { return fallback } }
  const writeJSON = (key, value) => { try { localStorage.setItem(key, JSON.stringify(value)); return true } catch { return false } }
  const firstShotId = SHOTS[0]?.shot_id || 'SH001'
  let review = readJSON(STORAGE_KEY, { schemaVersion: 5, shots: {}, knowledge: { cards: [] }, replications: {}, lastShot: firstShotId, savedAt: null })
  if (!review || typeof review !== 'object') review = { schemaVersion: 5, shots: {}, knowledge: { cards: [] }, replications: {}, lastShot: firstShotId, savedAt: null }
  if (!review.shots || typeof review.shots !== 'object') review.shots = {}
  review.schemaVersion = 5
  if (!review.knowledge || typeof review.knowledge !== 'object') review.knowledge = { cards: [] }
  if (!Array.isArray(review.knowledge.cards)) review.knowledge.cards = []
  if (!review.replications || typeof review.replications !== 'object' || Array.isArray(review.replications)) review.replications = {}
  let settings = readJSON(SETTINGS_KEY, { fontSize: 14, density: 'comfortable', railWidth: 238, inspectorWidth: 372, viewTone: 'long-review', aiEnabled: Boolean(config.aiDefaultEnabled), learningMode: 'learn' })
  if (!['long-review', 'bright'].includes(settings.viewTone)) settings.viewTone = 'long-review'
  if (typeof settings.aiEnabled !== 'boolean') settings.aiEnabled = Boolean(config.aiDefaultEnabled)
  if (!['learn', 'blind', 'efficient'].includes(settings.learningMode)) settings.learningMode = 'learn'
  let advisorHistory = readJSON(ADVISOR_STORAGE_KEY, { schemaVersion: 1, shots: {} })
  if (!advisorHistory || typeof advisorHistory !== 'object') advisorHistory = { schemaVersion: 1, shots: {} }
  if (!advisorHistory.shots || typeof advisorHistory.shots !== 'object') advisorHistory.shots = {}
  let selectedId = (location.hash.match(/SH\d{3,5}/i)?.[0] || review.lastShot || firstShotId).toUpperCase()
  if (!SHOTS.some((shot) => shot.shot_id === selectedId)) selectedId = firstShotId
  let filter = 'all'
  let query = ''
  let mediaMode = SHOTS[0]?.clip ? 'video' : 'dense'
  let inspectorTab = 'core'
  let toastTimer
  let aiPreview = null
  let advisorMode = 'open'
  let advisorHealth = { status: 'idle', provider: '', model: '', imageSupport: false, message: '' }
  let advisorRuntime = { loading: false, shotId: null, liveText: '', controller: null, provider: '', model: '', imageCount: 0, error: '' }
  let initialRuntime = { loading: false, shotId: null, error: '' }
  let replicationObjectUrl = ''

  const rawShot = (id) => SHOTS.find((shot) => shot.shot_id === id) || SHOTS[0]
  const reviewFor = (id) => {
    const state = review.shots[id] || (review.shots[id] = {})
    if (!['pending', 'issue', 'confirmed'].includes(state.status)) state.status = 'pending'
    if (!Array.isArray(state.checks)) state.checks = []
    state.checks = Array.from({ length: CHECKS.length }, (_, index) => Boolean(state.checks[index]))
    if (typeof state.notes !== 'string') state.notes = ''
    if (!state.patch || typeof state.patch !== 'object' || Array.isArray(state.patch)) state.patch = {}
    if (!Array.isArray(state.history)) state.history = []
    if (!state.learning || typeof state.learning !== 'object' || Array.isArray(state.learning)) state.learning = {}
    if (!state.learning.aiDraft || typeof state.learning.aiDraft !== 'object' || Array.isArray(state.learning.aiDraft)) state.learning.aiDraft = {}
    if (!state.learning.humanDraft || typeof state.learning.humanDraft !== 'object' || Array.isArray(state.learning.humanDraft)) state.learning.humanDraft = {}
    if (!state.learning.decisions || typeof state.learning.decisions !== 'object' || Array.isArray(state.learning.decisions)) state.learning.decisions = {}
    if (!state.learning.reasons || typeof state.learning.reasons !== 'object' || Array.isArray(state.learning.reasons)) state.learning.reasons = {}
    if (!state.learning.evidence || typeof state.learning.evidence !== 'object' || Array.isArray(state.learning.evidence)) state.learning.evidence = {}
    if (!Array.isArray(state.learning.corrections)) state.learning.corrections = []
    if (typeof state.learning.blindCompleted !== 'boolean') state.learning.blindCompleted = false
    if (typeof state.learning.aiEverVisible !== 'boolean') state.learning.aiEverVisible = false
    if (typeof state.learning.blindSessionValid !== 'boolean') state.learning.blindSessionValid = !state.learning.aiEverVisible
    if (!state.learning.blindSubmission || typeof state.learning.blindSubmission !== 'object' || Array.isArray(state.learning.blindSubmission)) state.learning.blindSubmission = {}
    if (typeof state.learning.revealed !== 'boolean') state.learning.revealed = settings.learningMode !== 'blind'
    if (!state.learning.meta || typeof state.learning.meta !== 'object') state.learning.meta = {}
    if (!('updatedAt' in state)) state.updatedAt = null
    return state
  }
  const effectiveShot = (shot) => ({ ...shot, ...(review.shots[shot.shot_id]?.patch || {}) })
  const selected = () => effectiveShot(rawShot(selectedId))
  const statusFor = (id) => review.shots[id]?.status || 'pending'
  const textPresent = (value, min = 1) => typeof value === 'string' && value.trim().length >= min
  const advisorFor = (id) => {
    const state = advisorHistory.shots[id] || (advisorHistory.shots[id] = { messages: [], updatedAt: null })
    if (!Array.isArray(state.messages)) state.messages = []
    return state
  }
  const replicationFor = (id) => {
    const state = review.replications[id] || (review.replications[id] = { target: '', taskNotes: '', result: null, scores: {}, notes: '', completedAt: null })
    if (!state.scores || typeof state.scores !== 'object') state.scores = {}
    return state
  }

  function saveAdvisorHistory() {
    writeJSON(ADVISOR_STORAGE_KEY, advisorHistory)
  }

  function save() {
    review.lastShot = selectedId
    review.savedAt = new Date().toISOString()
    writeJSON(STORAGE_KEY, review)
    updateCounts()
    updateSavedIndicator()
  }

  function toast(message) {
    const element = $('#toast')
    element.textContent = message
    element.classList.add('show')
    clearTimeout(toastTimer)
    toastTimer = setTimeout(() => element.classList.remove('show'), 1800)
  }

  function applySettings() {
    document.documentElement.style.setProperty('--body-size', `${settings.fontSize}px`)
    document.documentElement.style.setProperty('--rail-width', `${settings.railWidth}px`)
    document.documentElement.style.setProperty('--inspector-width', `${settings.inspectorWidth}px`)
    document.body.classList.toggle('compact', settings.density === 'compact')
    document.body.classList.toggle('ai-enabled', settings.aiEnabled)
    document.body.dataset.viewTone = settings.viewTone
    document.body.dataset.learningMode = settings.learningMode
    if (!settings.aiEnabled && inspectorTab === 'mentor') inspectorTab = 'core'
    $('#fontSize').value = String(settings.fontSize)
    $('#density').value = settings.density
    $('#railWidth').value = String(settings.railWidth)
    $('#inspectorWidth').value = String(settings.inspectorWidth)
    $('#viewTone').value = settings.viewTone
    $('#aiEnabled').value = settings.aiEnabled ? 'true' : 'false'
    $('#learningMode').value = settings.learningMode
  }

  function fieldCompleteness(shot) {
    const fields = Object.keys(FIELD_LABELS)
    const completed = fields.filter((key) => textPresent(shot[key]))
    return { completed: completed.length, total: fields.length, percent: Math.round(completed.length / fields.length * 100), missing: fields.filter((key) => !textPresent(shot[key])) }
  }

  function diagnoseShot(shot) {
    const warnings = []
    const add = (severity, code, title, detail, field) => warnings.push({ severity, code, title, detail, field })
    const combined = Object.keys(FIELD_LABELS).map((key) => shot[key] || '').join(' ')
    if (!textPresent(shot.literal_visual_description, 80)) add('high', 'literal-short', '整镜描述过短', '真实画面描述不足以覆盖起因、过程和结果。', 'literal_visual_description')
    if (!/F\d{3,4}/i.test(`${shot.boundary_evidence || ''} ${shot.timeline_beats || ''}`)) add('high', 'no-frame-evidence', '缺少逐帧证据', '切点或动作分拍没有明确帧号，难以复核时序。', 'timeline_beats')
    if (!/(肩|肘|腕|手|髋|膝|踝|脚|头|胸|腰|脊柱|重心|躯干)/.test(shot.body_part_specifics || '')) add('medium', 'body-vague', '肢体描述不够具体', '建议补充关节、支撑点与重心变化。', 'body_part_specifics')
    if (!/(方向|向左|向右|向前|向后|向上|向下|靠近|远离|推出|拉入|吸近|反向|接触点)/.test(shot.force_contact_response || '')) add('high', 'force-direction', '力向不明确', '接触点、力量来源或受力方向没有形成可重制约束。', 'force_contact_response')
    if (!/(固定|跟|推|拉|摇|移|环绕|升|降|变焦|拉焦|机位|镜头)/.test(`${shot.camera_motion || ''} ${shot.lens_fov || ''}`)) add('medium', 'camera-vague', '摄影机描述含糊', '应明确机位是否固定，以及主体尺寸变化是否来自人物位移。', 'camera_motion')
    if (/(变焦|推近|拉远|压缩|拉焦)/.test(combined) && !/(变焦|推近|拉远|拉焦|焦段变化)/.test(`${shot.camera_motion || ''} ${shot.lens_fov || ''}`)) add('high', 'camera-zoom-mismatch', '可能漏写变焦或焦段变化', '画面描述出现距离或空间压缩变化，但摄影机字段未明确对应手法。', 'camera_motion')
    const vfx = `${shot.vfx_layers_timing || ''} ${shot.interactive_lighting || ''} ${shot.environment_response || ''}`
    if (textPresent(vfx, 20) && !/(F\d{3,4}|先|随后|再|末段|瞬间|持续|开始|结束|命中|接触前|接触后)/.test(vfx)) add('medium', 'vfx-no-timing', 'VFX 缺少发生时机', '特效有内容，但没有明确出现、增强、反转或消散的顺序。', 'vfx_layers_timing')
    if (!textPresent(shot.previous_shot_result, 30) || !textPresent(shot.next_shot_bridge, 45)) add('high', 'bridge-thin', '前后镜桥接不足', '上一镜结果或下一镜开场证据不足，容易造成动作断裂。', 'next_shot_bridge')
    if (!textPresent(shot.uncertainties, 15) || /无不确定|没有不确定/.test(shot.uncertainties || '')) add('low', 'uncertainty-empty', '不确定项过少', '高速动作和遮挡镜头应明确哪些判断仍需人工复核。', 'uncertainties')
    const cloneText = `${shot.title || ''} ${shot.literal_visual_description || ''} ${shot.body_part_specifics || ''}`
    if (/分身/.test(cloneText) && /^\s*0\s*个?分身/.test(shot.clone_count_and_positions || '')) add('high', 'clone-conflict', '分身数量可能矛盾', '正文出现分身，但分身字段记录为 0。', 'clone_count_and_positions')
    const completeness = fieldCompleteness(shot)
    if (completeness.missing.length) add('high', 'missing-fields', `缺少 ${completeness.missing.length} 个字段`, completeness.missing.slice(0, 5).map((key) => FIELD_LABELS[key]).join('、'), completeness.missing[0])
    return warnings
  }

  function qualityScore(shot) {
    const completeness = fieldCompleteness(shot)
    const warnings = diagnoseShot(shot)
    const penalty = warnings.reduce((sum, item) => sum + (item.severity === 'high' ? 12 : item.severity === 'medium' ? 6 : 3), 0)
    return Math.max(0, Math.min(100, completeness.percent - penalty))
  }

  function reviewScore(id) {
    const checks = reviewFor(id).checks || []
    return Math.round(checks.filter(Boolean).length / CHECKS.length * 100)
  }

  function hasAttention(shot) {
    return statusFor(shot.shot_id) === 'issue' || diagnoseShot(effectiveShot(shot)).some((item) => item.severity !== 'low')
  }

  function updateCounts() {
    const states = SHOTS.map((shot) => statusFor(shot.shot_id))
    const confirmed = states.filter((state) => state === 'confirmed').length
    const issues = states.filter((state) => state === 'issue').length
    const pending = states.filter((state) => state === 'pending').length
    const attention = SHOTS.filter(hasAttention).length
    $('#confirmedCount').textContent = confirmed
    $('#pendingCount').textContent = pending
    $('#issueCount').textContent = issues
    if ($('#attentionCount')) $('#attentionCount').textContent = attention
    $('#totalCount').textContent = SHOTS.length
    $('#progressFill').style.width = `${confirmed / SHOTS.length * 100}%`
  }

  function updateSavedIndicator() {
    const target = $('#savedIndicator')
    if (!target) return
    if (!review.savedAt) { target.textContent = '尚未产生审核修改'; return }
    const time = new Date(review.savedAt)
    target.textContent = `已保存 ${time.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`
  }

  function filteredShots() {
    const normalizedQuery = query.trim().toLowerCase()
    return SHOTS.filter((raw) => {
      const shot = effectiveShot(raw)
      const state = statusFor(shot.shot_id)
      if (filter === 'attention' && !hasAttention(raw)) return false
      if (filter !== 'all' && filter !== 'attention' && state !== filter) return false
      if (!normalizedQuery) return true
      const haystack = [shot.shot_id, shot.title, shot.phase, ...Object.keys(FIELD_LABELS).map((key) => shot[key])].join(' ').toLowerCase()
      return haystack.includes(normalizedQuery)
    })
  }

  function renderList() {
    const list = filteredShots()
    $('#visibleCount').textContent = list.length
    $('#shotList').innerHTML = list.length ? list.map((shot) => {
      const state = statusFor(shot.shot_id)
      const warnings = diagnoseShot(shot).filter((item) => item.severity !== 'low')
      const score = qualityScore(shot)
      const statusText = state === 'confirmed' ? '已确认' : state === 'issue' ? '有问题' : '待审核'
      const decisions = Object.keys(reviewFor(shot.shot_id).learning.decisions).length
      const learningText = decisions ? ` · 学习 ${decisions}/${LEARNING_FIELDS.length}` : ''
      return `<button class="shot-item ${shot.shot_id === selectedId ? 'on' : ''}" data-id="${shot.shot_id}"><span class="shot-num">${shot.shot_id.slice(2)}</span><span class="shot-copy"><b>${escapeHTML(shot.title)}</b><small>质量 ${score} · ${warnings.length ? `${warnings.length} 项诊断` : statusText}${learningText}</small></span><i class="state ${state} ${warnings.length ? 'attention' : ''}"></i></button>`
    }).join('') : '<div class="rail-empty">没有匹配的镜头</div>'
    $$('.shot-item').forEach((button) => button.addEventListener('click', () => selectShot(button.dataset.id)))
    requestAnimationFrame(() => $('.shot-item.on')?.scrollIntoView({ block: 'nearest' }))
  }

  function selectShot(id, { preserveScroll = false } = {}) {
    const oldVideo = $('#mediaView video')
    if (oldVideo) oldVideo.pause()
    if (advisorRuntime.loading && advisorRuntime.shotId !== id) advisorRuntime.controller?.abort()
    if (replicationObjectUrl) { URL.revokeObjectURL(replicationObjectUrl); replicationObjectUrl = '' }
    selectedId = id
    const learning = reviewFor(id).learning
    if (settings.learningMode === 'blind' && !learning.blindCompleted) {
      learning.blindSessionValid = !learning.aiEverVisible
      learning.revealed = false
    }
    location.hash = id
    save()
    renderList()
    renderShot()
    if (!preserveScroll) window.scrollTo({ top: 0, behavior: 'auto' })
  }

  function renderShot() {
    const shot = selected()
    const state = reviewFor(shot.shot_id)
    const diagnostics = diagnoseShot(shot)
    const shotTitle = `${shot.shot_id} · ${shot.title}`
    const shotMeta = `F${String(shot.source_in_frame).padStart(4, '0')}—F${String(shot.source_out_frame).padStart(4, '0')}（出点开区间）　·　${shot.source_in}—${shot.source_out}　·　${Number(shot.duration_sec).toFixed(3)}s　·　${shot.boundary_type}`
    $('#shotTitle').textContent = shotTitle
    $('#shotTitle').title = shotTitle
    $('#shotMeta').textContent = shotMeta
    $('#shotMeta').title = shotMeta
    $('#confidence').textContent = diagnostics.length ? `质量 ${qualityScore(shot)} · ${diagnostics.length} 项诊断` : `质量 ${qualityScore(shot)} · 无阻断项`
    $('#confidence').classList.toggle('has-warning', diagnostics.some((item) => item.severity === 'high'))
    renderMedia()
    renderBeats()
    renderCausal()
    renderInspector()
    updateActionButtons(state)
  }

  function renderMedia() {
    const shot = selected()
    const view = $('#mediaView')
    if (mediaMode === 'video' && !shot.clip) mediaMode = 'dense'
    $$('.media-tab').forEach((button) => {
      button.classList.toggle('on', button.dataset.media === mediaMode)
      if (button.dataset.media === 'video') button.disabled = !shot.clip
    })
    if (mediaMode === 'video') {
      view.innerHTML = `<video id="reviewVideo" controls preload="metadata" poster="${escapeHTML(shot.middle_frame)}" src="${escapeHTML(shot.clip)}"></video>`
      const video = $('#reviewVideo')
      video.playbackRate = Number($('#playbackRate').textContent.replace('×', '')) || 0.5
      video.muted = $('#muteBtn').textContent === '静音'
    } else if (mediaMode === 'dense') {
      view.innerHTML = `<img src="${escapeHTML(shot.dense_contact_sheet)}" alt="${shot.shot_id} 逐帧接触表"><a class="sheet-link" target="_blank" href="${escapeHTML(shot.dense_contact_sheet)}">查看原图 ↗</a>`
    } else {
      view.innerHTML = `<div class="frame-grid"><figure><img src="${escapeHTML(shot.start_frame)}" alt="首帧"><figcaption>首帧 · F${String(shot.source_in_frame).padStart(4, '0')}</figcaption></figure><figure><img src="${escapeHTML(shot.middle_frame)}" alt="中间帧"><figcaption>中间帧</figcaption></figure><figure><img src="${escapeHTML(shot.end_frame)}" alt="尾帧"><figcaption>尾帧 · F${String(Math.max(shot.source_in_frame, shot.source_out_frame - 1)).padStart(4, '0')}</figcaption></figure></div>`
    }
  }

  function renderBeats() {
    const shot = selected()
    let beats = String(shot.timeline_beats || '').split(/[；;]/).map((item) => item.trim()).filter(Boolean)
    if (!beats.length) beats = [shot.opening_frame_literal, shot.literal_visual_description, shot.end_state]
    const images = [shot.start_frame, shot.middle_frame, shot.end_frame, shot.dense_contact_sheet]
    $('#beatStrip').innerHTML = beats.slice(0, 4).map((beat, index) => {
      const frame = (beat.match(/F\d{3,4}(?:[–—-]F?\d{3,4})?/) || [])[0] || ['起点', '发展', '结果', '衔接'][index]
      return `<div class="beat"><img loading="lazy" src="${escapeHTML(images[Math.min(index, images.length - 1)])}" alt=""><div class="beat-copy"><b>${escapeHTML(frame)}</b><span>${escapeHTML(beat.replace(frame, '').trim())}</span></div></div>`
    }).join('')
  }

  function renderCausal() {
    const shot = selected()
    $('#causal').innerHTML = `<div class="causal-title">镜头因果链</div><div class="cause"><b>上一镜结果</b><span title="${escapeHTML(shot.previous_shot_result)}">${escapeHTML(truncate(shot.previous_shot_result, 100))}</span></div><span class="arrow">→</span><div class="cause"><b>本镜动作</b><span title="${escapeHTML(shot.literal_visual_description)}">${escapeHTML(truncate(shot.literal_visual_description, 145))}</span></div><span class="arrow">→</span><div class="cause"><b>下一镜桥接</b><span title="${escapeHTML(shot.next_shot_bridge)}">${escapeHTML(truncate(shot.next_shot_bridge, 100))}</span></div>`
  }

  function fieldBlock(type, label, text, confidence = '') {
    const className = type === '待核' ? 'question' : type === '判断' ? 'judge' : type === '适配' ? 'adapt' : 'fact'
    return `<section class="fact-block ${type === '待核' ? 'issue' : ''}"><div class="fact-label"><span class="type ${className}">${type}</span><span class="category">${escapeHTML(label)}</span>${confidence ? `<span class="field-confidence">${escapeHTML(confidence)}</span>` : ''}</div><p>${escapeHTML(text)}</p></section>`
  }

  function progressSummary(shot) {
    const fields = fieldCompleteness(shot)
    const quality = qualityScore(shot)
    const human = reviewScore(shot.shot_id)
    return `<div class="quality-summary"><div><span>字段覆盖</span><b>${fields.percent}%</b></div><div><span>自动质量</span><b>${quality}%</b></div><div><span>人工确认</span><b>${human}%</b></div></div>`
  }

  function diagnosticsHTML(shot) {
    const diagnostics = diagnoseShot(shot)
    if (!diagnostics.length) return '<div class="diagnostic-ready"><b>自动诊断通过</b><span>未发现结构性缺项，仍需人工核对真实画面。</span></div>'
    return `<div class="diagnostic-list">${diagnostics.map((item) => `<button type="button" class="diagnostic-item severity-${item.severity}" data-field="${escapeHTML(item.field || '')}"><span>${item.severity === 'high' ? '高' : item.severity === 'medium' ? '中' : '低'}</span><div><b>${escapeHTML(item.title)}</b><p>${escapeHTML(item.detail)}</p></div></button>`).join('')}</div>`
  }

  function advisorInlineMarkup(text) {
    return escapeHTML(text)
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/`([^`]+)`/g, '<code>$1</code>')
  }

  function advisorTextMarkup(text) {
    return String(text || '').split(/\r?\n/).map((line) => {
      const trimmed = line.trim()
      if (!trimmed) return '<div class="advisor-gap"></div>'
      if (trimmed.startsWith('### ')) return `<h4>${advisorInlineMarkup(trimmed.slice(4))}</h4>`
      if (/^[-*]\s+/.test(trimmed)) return `<div class="advisor-list-item"><i></i><span>${advisorInlineMarkup(trimmed.replace(/^[-*]\s+/, ''))}</span></div>`
      return `<p>${advisorInlineMarkup(trimmed)}</p>`
    }).join('')
  }

  function advisorShotSummary(shot) {
    if (!shot) return null
    return {
      shot_id: shot.shot_id,
      title: shot.title,
      opening_frame_literal: shot.opening_frame_literal,
      screen_layout: shot.screen_layout,
      literal_visual_description: shot.literal_visual_description,
      camera_position: shot.camera_position,
      camera_motion: shot.camera_motion,
      lens_fov: shot.lens_fov,
      focus_depth: shot.focus_depth,
      interactive_lighting: shot.interactive_lighting,
      vfx_layers_timing: shot.vfx_layers_timing,
      end_state: shot.end_state,
    }
  }

  function buildAdvisorContext(shot) {
    const index = SHOTS.findIndex((item) => item.shot_id === shot.shot_id)
    const previous = index > 0 ? effectiveShot(SHOTS[index - 1]) : null
    const next = index < SHOTS.length - 1 ? effectiveShot(SHOTS[index + 1]) : null
    const assetPath = (path) => {
      if (!path) return ''
      return [String(PROJECT.advisor_asset_prefix || '').replace(/^\/+|\/+$/g, ''), String(path).replace(/^\/+/, '')].filter(Boolean).join('/')
    }
    return {
      shot_id: shot.shot_id,
      title: shot.title,
      phase: shot.phase,
      source_in_frame: shot.source_in_frame,
      source_out_frame: shot.source_out_frame,
      source_in: shot.source_in,
      source_out: shot.source_out,
      duration_sec: shot.duration_sec,
      analysis_confidence: shot.analysis_confidence,
      boundary_type: shot.boundary_type,
      evidence: Object.fromEntries(Object.keys(FIELD_LABELS).map((key) => [key, shot[key] || ''])),
      diagnostics: diagnoseShot(shot).map(({ severity, title, detail, field }) => ({ severity, title, detail, field })),
      previous_shot: advisorShotSummary(previous),
      next_shot: advisorShotSummary(next),
      assets: {
        start_frame: assetPath(shot.start_frame),
        middle_frame: assetPath(shot.middle_frame),
        end_frame: assetPath(shot.end_frame),
        dense_contact_sheet: assetPath(shot.dense_contact_sheet),
        clip: assetPath(shot.clip),
      },
    }
  }

  function reasonLabel(value) {
    return CORRECTION_REASONS.find(([key]) => key === value)?.[1] || value || '未填写'
  }

  function trustedMemoryFor(shot) {
    const text = `${shot.title || ''} ${shot.camera_motion || ''} ${shot.timeline_beats || ''} ${shot.vfx_layers_timing || ''}`
    return [...review.knowledge.cards]
      .filter((card) => card && card.status === 'trusted')
      .map((card) => ({ card, score: LEARNING_FIELDS.includes(card.field) ? 2 : 0, hit: text.includes(card.conclusion?.slice(0, 8) || '___') ? 1 : 0 }))
      .sort((a, b) => (b.score + b.hit) - (a.score + a.hit) || String(b.card.createdAt).localeCompare(String(a.card.createdAt)))
      .slice(0, 8)
      .map(({ card }) => ({ id: card.id, field: card.field, conclusion: card.conclusion, reason: card.reasonLabel || card.reason, evidence: card.evidence }))
  }

  function learningMetrics() {
    const states = SHOTS.map((shot) => reviewFor(shot.shot_id).learning)
    const corrections = states.flatMap((state) => state.corrections || [])
    const replications = Object.values(review.replications).filter((item) => item?.completedAt)
    return {
      draftedShots: states.filter((state) => Object.keys(state.aiDraft || {}).length).length,
      decisions: states.reduce((sum, state) => sum + Object.keys(state.decisions || {}).length, 0),
      corrections: corrections.filter((item) => item.outcome === 'corrected').length,
      blindDecisions: corrections.filter((item) => item.mode === 'blind').length,
      knowledge: review.knowledge.cards.filter((card) => card.status === 'trusted').length,
      replications: replications.length,
    }
  }

  async function requestInitialAnalysis() {
    if (!settings.aiEnabled) { toast('AI 是可选功能，可在“舒适度”中启用'); return }
    const shot = selected()
    const state = reviewFor(shot.shot_id)
    if (initialRuntime.loading) return
    if (Object.keys(state.learning.decisions).length && !window.confirm('重新初拉会保留已确认结论，但替换当前 AI 初判。继续吗？')) return
    initialRuntime = { loading: true, shotId: shot.shot_id, error: '' }
    renderInspector()
    try {
      const response = await fetch(INITIAL_ANALYSIS_ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          request_id: `initial-${Date.now()}`,
          shot_id: shot.shot_id,
          fields: LEARNING_FIELDS,
          context: buildAdvisorContext(shot),
          trusted_memory: trustedMemoryFor(shot),
        }),
      })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`)
      if (String(data.shot_id || '').toUpperCase() !== shot.shot_id) throw new Error('AI 初拉返回了错误的镜头编号')
      const draft = {}
      for (const field of LEARNING_FIELDS) {
        const item = data.fields?.[field]
        if (!item || typeof item.value !== 'string' || !item.value.trim()) continue
        draft[field] = {
          value: item.value.trim(),
          confidence: ['low', 'medium', 'high'].includes(item.confidence) ? item.confidence : 'low',
          evidence: String(item.evidence || '').trim(),
        }
      }
      if (!Object.keys(draft).length) throw new Error('AI 初拉没有返回可审核字段')
      state.learning.aiDraft = draft
      state.learning.revealed = settings.learningMode !== 'blind'
      if (settings.learningMode !== 'blind') state.learning.aiEverVisible = true
      if (settings.learningMode === 'blind' && typeof state.learning.blindSessionValid !== 'boolean') state.learning.blindSessionValid = !state.learning.aiEverVisible
      state.learning.meta = {
        provider: data.provider || '', model: data.model || '', imageCount: Number(data.image_count || 0),
        generatedAt: data.generated_at || new Date().toISOString(), memoryUsed: Array.isArray(data.memory_used) ? data.memory_used : [],
        uncertainties: Array.isArray(data.uncertainties) ? data.uncertainties : [],
      }
      state.status = 'pending'
      state.updatedAt = new Date().toISOString()
      save()
      renderList()
      toast(settings.learningMode === 'blind' ? `${shot.shot_id} AI 初判已生成并隐藏` : `${shot.shot_id} AI 初拉完成`)
    } catch (error) {
      initialRuntime.error = error.message || 'AI 初拉失败'
    } finally {
      initialRuntime.loading = false
      if (selectedId === shot.shot_id && inspectorTab === 'learning') renderInspector()
    }
  }

  function decideLearningField(field, outcome) {
    const shot = selected()
    const state = reviewFor(shot.shot_id)
    const learning = state.learning
    const ai = learning.aiDraft[field]
    const human = String(learning.humanDraft[field] || '').trim()
    if (outcome === 'accepted' && (!ai || (settings.learningMode === 'blind' && !learning.revealed))) return
    if (outcome === 'corrected' && !human) { toast('先填写你的判断'); return }
    if (outcome === 'accepted' && !learning.reasons[field]) learning.reasons[field] = 'accepted_with_evidence'
    const reason = learning.reasons[field] || ''
    const evidence = String(learning.evidence[field] || '').trim()
    if (!reason) { toast('先选择纠错原因'); return }
    if (!evidence) { toast('先填写证据帧或时间段'); return }
    const finalValue = outcome === 'accepted' ? ai.value : human
    const blindSubmission = String(learning.blindSubmission[field] || '').trim()
    const independentBlind = settings.learningMode === 'blind'
      && learning.blindSessionValid
      && (!learning.revealed || (blindSubmission && blindSubmission === human))
    const at = new Date().toISOString()
    const decision = {
      outcome,
      aiValue: ai?.value || '',
      humanValue: human,
      finalValue,
      reason,
      evidence,
      mode: settings.learningMode === 'blind' ? (independentBlind ? 'blind' : 'blind_after_ai') : settings.learningMode,
      at,
    }
    learning.decisions[field] = decision
    learning.corrections = [...learning.corrections, { field, ...decision }].slice(-80)
    state.patch[field] = finalValue
    state.status = 'pending'
    state.updatedAt = at
    save()
    renderList()
    renderShot()
    toast(outcome === 'accepted' ? `${FIELD_LABELS[field]}已采用 AI 判断` : `${FIELD_LABELS[field]}已采用人工判断`)
  }

  function promoteKnowledge(field) {
    const shot = selected()
    const state = reviewFor(shot.shot_id)
    const learning = state.learning
    const decision = learning.decisions[field]
    const reason = learning.reasons[field] || decision?.reason || ''
    const evidence = String(learning.evidence[field] || decision?.evidence || '').trim()
    if (!decision) { toast('先形成最终结论'); return }
    if (!reason || !evidence) { toast('沉淀前必须补齐纠错原因和证据'); return }
    const duplicate = review.knowledge.cards.some((card) => card.sourceShotId === shot.shot_id && card.field === field && card.decisionAt === decision.at)
    if (duplicate) { toast('这条结论已经进入可信知识'); return }
    review.knowledge.cards.push({
      id: `K-${shot.shot_id}-${field}-${Date.now().toString(36)}`,
      status: 'trusted',
      field,
      fieldLabel: FIELD_LABELS[field] || field,
      conclusion: decision.finalValue,
      aiValue: decision.aiValue,
      reason,
      reasonLabel: reasonLabel(reason),
      evidence,
      sourceShotId: shot.shot_id,
      sourceTitle: shot.title,
      reviewMode: decision.mode,
      decisionAt: decision.at,
      createdAt: new Date().toISOString(),
    })
    save()
    renderInspector()
    toast('已由人工晋级为可信知识')
  }

  function advisorStatusHTML() {
    const status = advisorHealth.status
    const label = status === 'online'
      ? `${advisorHealth.provider || 'AI'} · ${advisorHealth.model || '已连接'}${advisorHealth.imageSupport ? ' · 可附带帧图' : ' · 文字上下文'}`
      : status === 'checking' ? '正在连接本地 AI 网关…'
        : status === 'offline' ? 'AI 服务未启动' : '尚未检查 AI 服务'
    return `<button type="button" class="advisor-status ${status}" id="advisorReconnect" title="重新检查连接"><i></i><span>${escapeHTML(label)}</span></button>`
  }

  function advisorPanelHTML(shot) {
    const state = advisorFor(shot.shot_id)
    const isLive = advisorRuntime.loading && advisorRuntime.shotId === shot.shot_id
    const messages = state.messages.map((message, index) => {
      const assistant = message.role === 'assistant'
      const meta = assistant && (message.provider || message.model) ? `<span>${escapeHTML([message.provider, message.model, message.imageCount ? `${message.imageCount} 图` : ''].filter(Boolean).join(' · '))}</span>` : ''
      return `<article class="advisor-message ${assistant ? 'assistant' : 'user'}"><div class="advisor-message-head"><b>${assistant ? 'AI 影像导师' : '你的描述'}</b>${meta}</div><div class="advisor-message-body">${advisorTextMarkup(message.text)}</div>${assistant ? `<div class="advisor-message-actions"><button type="button" data-advisor-note="${index}">追加到人工备注</button></div>` : ''}</article>`
    }).join('')
    const live = isLive ? `<article class="advisor-message assistant live"><div class="advisor-message-head"><b>AI 影像导师</b><span id="advisorLiveMeta">正在分析镜头证据…</span></div><div class="advisor-message-body" id="advisorLiveText">${advisorTextMarkup(advisorRuntime.liveText || '正在建立专业判断…')}</div></article>` : ''
    const empty = !messages && !live ? '<div class="advisor-empty"><b>可以直接说人话，不需要先懂术语</b><p>例如：“镜头没动，但人越来越大，背景好像被压扁了。”导师会区分推镜、变焦、主体位移和后期裁切，并继续追问关键证据。</p></div>' : ''
    const offline = advisorHealth.status === 'offline' ? `<div class="advisor-offline"><b>先启动本地网关</b><code>python3 src/ai_advisor_server.py --provider mock</code><span>${escapeHTML(advisorHealth.message || '真实模型的密钥只配置在服务端环境变量。')}</span></div>` : ''
    return `<div class="advisor-panel"><div class="advisor-head"><div><b>当前镜头专业问答</b><span>自动附带 ${shot.shot_id}、前后镜、诊断和帧图路径</span></div>${advisorStatusHTML()}</div>${offline}<div class="advisor-quick">${ADVISOR_QUICK_QUESTIONS.map(([mode, text]) => `<button type="button" data-advisor-quick="${escapeHTML(text)}" data-advisor-mode="${mode}">${escapeHTML(text.replace(/[？?]$/, ''))}</button>`).join('')}</div><div class="advisor-chat">${empty}${messages}${live}${advisorRuntime.error && advisorRuntime.shotId === shot.shot_id ? `<div class="advisor-error">${escapeHTML(advisorRuntime.error)}</div>` : ''}</div><div class="advisor-composer"><div class="advisor-composer-row"><select id="advisorMode" aria-label="提问类型">${ADVISOR_MODES.map(([value, label]) => `<option value="${value}" ${advisorMode === value ? 'selected' : ''}>${label}</option>`).join('')}</select><button type="button" class="advisor-clear" id="advisorClear" ${state.messages.length ? '' : 'disabled'}>清空对话</button></div><textarea id="advisorQuestion" placeholder="用你自己的话描述疑问。越不确定越可以直接说出来…" ${isLive ? 'disabled' : ''}></textarea><div class="advisor-send-row"><span>⌘ / Ctrl + Enter 发送</span>${isLive ? '<button type="button" class="btn" id="advisorStop">停止</button>' : '<button type="button" class="btn primary" id="advisorSend">发送问题</button>'}</div></div></div>`
  }

  function bindAdvisorPanel(shot) {
    $$('[data-advisor-quick]').forEach((button) => button.addEventListener('click', () => {
      advisorMode = button.dataset.advisorMode || 'open'
      $('#advisorMode').value = advisorMode
      $('#advisorQuestion').value = button.dataset.advisorQuick
      $('#advisorQuestion').focus()
    }))
    $('#advisorMode').addEventListener('change', (event) => { advisorMode = event.target.value })
    const question = $('#advisorQuestion')
    question?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) { event.preventDefault(); sendAdvisorQuestion() }
    })
    $('#advisorSend')?.addEventListener('click', sendAdvisorQuestion)
    $('#advisorStop')?.addEventListener('click', stopAdvisor)
    $('#advisorReconnect')?.addEventListener('click', () => checkAdvisorHealth(true))
    $('#advisorClear')?.addEventListener('click', () => clearAdvisorHistory(shot.shot_id))
    $$('[data-advisor-note]').forEach((button) => button.addEventListener('click', () => appendAdvisorToNotes(shot.shot_id, Number(button.dataset.advisorNote))))
    if (advisorHealth.status === 'idle') setTimeout(() => checkAdvisorHealth(), 0)
  }

  function scrollAdvisorToBottom() {
    const scroll = $('#inspectorContent')
    if (scroll) scroll.scrollTop = scroll.scrollHeight
  }

  async function checkAdvisorHealth(force = false) {
    if (!settings.aiEnabled) return
    if (advisorHealth.status === 'checking' || (advisorHealth.status === 'online' && !force)) return
    advisorHealth = { status: 'checking', provider: '', model: '', imageSupport: false, message: '' }
    if (inspectorTab === 'mentor') renderInspector()
    try {
      const response = await fetch(ADVISOR_HEALTH_ENDPOINT, { cache: 'no-store' })
      const data = await response.json()
      if (!response.ok || !data.ok) throw new Error(data.error || `HTTP ${response.status}`)
      advisorHealth = { status: 'online', provider: data.provider || '', model: data.model || '', imageSupport: Boolean(data.vision_assets), message: '' }
    } catch (error) {
      advisorHealth = { status: 'offline', provider: '', model: '', imageSupport: false, message: error.message || '无法连接本地 AI 网关' }
    }
    if (inspectorTab === 'mentor') renderInspector()
  }

  function handleAdvisorStreamEvent(event, finalState) {
    if (!event || typeof event !== 'object') return
    if (event.type === 'meta') {
      advisorRuntime.provider = event.provider || ''
      advisorRuntime.model = event.model || ''
      advisorRuntime.imageCount = Number(event.image_count || 0)
      const liveMeta = $('#advisorLiveMeta')
      if (liveMeta) liveMeta.textContent = [advisorRuntime.provider, advisorRuntime.model, advisorRuntime.imageCount ? `${advisorRuntime.imageCount} 张帧图` : '文字上下文'].filter(Boolean).join(' · ')
    } else if (event.type === 'delta') {
      advisorRuntime.liveText += String(event.text || '')
      const liveText = $('#advisorLiveText')
      if (liveText) liveText.innerHTML = advisorTextMarkup(advisorRuntime.liveText)
      requestAnimationFrame(scrollAdvisorToBottom)
    } else if (event.type === 'done') {
      finalState.answer = String(event.answer || advisorRuntime.liveText || '').trim()
    } else if (event.type === 'error') {
      throw new Error(event.message || 'AI 服务返回错误')
    }
  }

  async function sendAdvisorQuestion() {
    if (!settings.aiEnabled) { toast('AI 影像导师尚未启用'); return }
    const input = $('#advisorQuestion')
    const question = input?.value.trim()
    if (!question || advisorRuntime.loading) return
    const shot = selected()
    const state = advisorFor(shot.shot_id)
    const conversation = state.messages.slice(-12).map(({ role, text }) => ({ role, text }))
    state.messages.push({ role: 'user', text: question, at: new Date().toISOString() })
    state.messages = state.messages.slice(-30)
    state.updatedAt = new Date().toISOString()
    saveAdvisorHistory()
    const controller = new AbortController()
    advisorRuntime = { loading: true, shotId: shot.shot_id, liveText: '', controller, provider: '', model: '', imageCount: 0, error: '' }
    renderInspector()
    const finalState = { answer: '' }
    try {
      const response = await fetch(ADVISOR_ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: controller.signal,
        body: JSON.stringify({
          schema_version: '1.0',
          request_id: globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`,
          shot_id: shot.shot_id,
          mode: advisorMode,
          question,
          conversation,
          context: buildAdvisorContext(shot),
        }),
      })
      if (!response.ok) {
        let detail = `HTTP ${response.status}`
        try { detail = (await response.json()).error || detail } catch {}
        throw new Error(detail)
      }
      if (!response.body) throw new Error('浏览器未获得流式响应')
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      while (true) {
        const { value, done } = await reader.read()
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''
        for (const line of lines) {
          if (!line.trim()) continue
          handleAdvisorStreamEvent(JSON.parse(line), finalState)
        }
        if (done) break
      }
      if (buffer.trim()) handleAdvisorStreamEvent(JSON.parse(buffer), finalState)
      const answer = finalState.answer || advisorRuntime.liveText.trim()
      if (!answer) throw new Error('AI 没有返回有效内容')
      state.messages.push({ role: 'assistant', text: answer, provider: advisorRuntime.provider, model: advisorRuntime.model, imageCount: advisorRuntime.imageCount, at: new Date().toISOString() })
      state.messages = state.messages.slice(-30)
      state.updatedAt = new Date().toISOString()
      advisorHealth = { status: 'online', provider: advisorRuntime.provider || advisorHealth.provider, model: advisorRuntime.model || advisorHealth.model, imageSupport: advisorRuntime.imageCount > 0 || advisorHealth.imageSupport, message: '' }
      saveAdvisorHistory()
    } catch (error) {
      if (error.name !== 'AbortError') advisorRuntime.error = error.message || 'AI 问答失败'
    } finally {
      const error = advisorRuntime.error
      advisorRuntime = { loading: false, shotId: shot.shot_id, liveText: '', controller: null, provider: '', model: '', imageCount: 0, error }
      if (selectedId === shot.shot_id && inspectorTab === 'mentor') renderInspector()
    }
  }

  function stopAdvisor() {
    advisorRuntime.controller?.abort()
    advisorRuntime = { loading: false, shotId: selectedId, liveText: '', controller: null, provider: '', model: '', imageCount: 0, error: '本次回答已停止。' }
    renderInspector()
  }

  function appendAdvisorToNotes(shotId, index) {
    const message = advisorFor(shotId).messages[index]
    if (!message || message.role !== 'assistant') return
    const state = reviewFor(shotId)
    const block = `【AI 影像导师 · 待人工核对】\n${message.text}`
    if (!state.notes.includes(block)) state.notes = [state.notes.trim(), block].filter(Boolean).join('\n\n')
    state.updatedAt = new Date().toISOString()
    save()
    toast('AI 解答已追加到人工备注，不会自动修改事实字段')
  }

  function clearAdvisorHistory(shotId) {
    if (!advisorFor(shotId).messages.length) return
    if (!window.confirm(`清空 ${shotId} 的 AI 导师对话？这不会删除人工备注。`)) return
    advisorHistory.shots[shotId] = { messages: [], updatedAt: new Date().toISOString() }
    saveAdvisorHistory()
    renderInspector()
  }

  function learningPanelHTML(shot) {
    const state = reviewFor(shot.shot_id)
    const learning = state.learning
    if (settings.learningMode === 'blind' && !learning.blindCompleted) learning.revealed = false
    const draftCount = Object.keys(learning.aiDraft).length
    const decisionCount = Object.keys(learning.decisions).length
    const trustedCount = review.knowledge.cards.filter((card) => card.sourceShotId === shot.shot_id && card.status === 'trusted').length
    const modeCopy = {
      learn: '先查看 AI 初判，再依据原片逐项接受或纠正。',
      blind: '先写下独立判断；AI 初判在提交对照前保持隐藏。',
      efficient: '只处理低可信度、自动诊断和尚未形成结论的字段。',
    }[settings.learningMode]
    const loading = initialRuntime.loading && initialRuntime.shotId === shot.shot_id
    const buttonLabel = loading ? '正在初拉…' : draftCount ? '重新 AI 初拉' : 'AI 初拉当前镜头'
    const headerButton = `<button type="button" class="btn" id="initialAnalyze" ${!settings.aiEnabled || loading ? 'disabled' : ''}>${settings.aiEnabled ? buttonLabel : 'AI 未启用'}</button>`
    const blindLocked = settings.learningMode === 'blind' && draftCount > 0 && !learning.revealed
    const humanCount = LEARNING_FIELDS.filter((field) => String(learning.humanDraft[field] || '').trim()).length
    const blindValid = learning.blindSessionValid !== false
    const blindGate = blindLocked ? `<div class="blind-gate"><b>${blindValid ? 'AI 初判已生成并隐藏' : '本镜已经看过 AI 初判'}</b><p>${blindValid ? '只有揭示前已写下且之后未改写的字段计入有效盲审。' : '仍可隐藏后对照，但本次决定不会计入有效盲审数据。'}</p><button type="button" class="btn" id="revealBlind" ${humanCount ? '' : 'disabled'}>提交${blindValid ? '盲审' : '判断'}并对照 AI</button></div>` : ''
    const diagnosticFields = new Set(diagnoseShot(shot).map((item) => item.field))
    const visibleFields = LEARNING_FIELDS.filter((field) => {
      if (settings.learningMode !== 'efficient') return true
      const ai = learning.aiDraft[field]
      return !learning.decisions[field] && (!ai || ai.confidence === 'low' || diagnosticFields.has(field))
    })
    const fieldsHTML = visibleFields.map((field) => {
      const ai = learning.aiDraft[field]
      const decision = learning.decisions[field]
      const promoted = review.knowledge.cards.some((card) => card.sourceShotId === shot.shot_id && card.field === field && card.decisionAt === decision?.at)
      const confidence = ai?.confidence || 'low'
      const confidenceLabel = ai ? ({ low: '低可信', medium: '中可信', high: '高可信' })[confidence] : '无初判'
      const status = decision ? (decision.outcome === 'accepted' ? '已接受 AI' : '已采用人工') : '待判断'
      const aiContent = blindLocked
        ? '<div class="ai-draft blind">AI 内容已隐藏</div>'
        : ai
          ? `<div class="ai-draft">${escapeHTML(ai.value)}${ai.evidence ? `<span class="ai-evidence">依据：${escapeHTML(ai.evidence)}</span>` : ''}</div>`
          : '<div class="ai-draft blind">尚未生成 AI 初判</div>'
      const options = CORRECTION_REASONS.map(([value, label]) => `<option value="${value}" ${learning.reasons[field] === value ? 'selected' : ''}>${escapeHTML(label)}</option>`).join('')
      return `<section class="learning-field" data-learning-field="${field}">
        <div class="learning-field-head"><b>${escapeHTML(FIELD_LABELS[field] || field)}</b><span class="confidence-tag ${confidence}">${confidenceLabel}</span><span class="learning-status">${status}</span></div>
        ${aiContent}
        <label class="learning-label" for="human-${field}">你的判断</label>
        <textarea class="human-draft" id="human-${field}" data-human-field="${field}" placeholder="依据视频和逐帧证据写下你的判断…">${escapeHTML(learning.humanDraft[field] || '')}</textarea>
        <div class="learning-meta"><select data-reason-field="${field}" aria-label="${escapeHTML(FIELD_LABELS[field])}纠错原因">${options}</select><input data-evidence-field="${field}" value="${escapeHTML(learning.evidence[field] || '')}" placeholder="证据帧 / 时间段"></div>
        <div class="learning-actions"><button type="button" data-accept-field="${field}" ${!ai || blindLocked ? 'disabled' : ''}>AI 判断可用</button><button type="button" class="primary" data-correct-field="${field}" ${String(learning.humanDraft[field] || '').trim() ? '' : 'disabled'}>采用人工判断</button>${decision ? `<button type="button" class="promote" data-promote-field="${field}" ${promoted ? 'disabled' : ''}>${promoted ? '已沉淀' : '沉淀知识'}</button>` : ''}</div>
      </section>`
    }).join('')
    const empty = settings.learningMode === 'efficient' && !visibleFields.length ? '<div class="learning-empty"><b>当前高风险字段已处理</b><p>切换到学习模式可以查看全部人机判断记录。</p></div>' : ''
    const meta = learning.meta.provider ? `${learning.meta.provider} · ${learning.meta.model || 'model'}${learning.meta.imageCount ? ` · ${learning.meta.imageCount} 图` : ''}` : '尚无 AI 初拉'
    const error = initialRuntime.error && initialRuntime.shotId === shot.shot_id ? `<div class="learning-error">${escapeHTML(initialRuntime.error)}</div>` : ''
    return `<div class="learning-panel"><div class="learning-head"><div class="learning-head-row"><div><b>${escapeHTML(({ learn: '学习模式', blind: '盲审模式', efficient: '效率模式' })[settings.learningMode])}</b><p>${escapeHTML(modeCopy)}<br>${escapeHTML(meta)}</p></div>${headerButton}</div></div><div class="learning-summary"><div><span>AI 初判</span><b>${draftCount}</b></div><div><span>最终结论</span><b>${decisionCount}</b></div><div><span>可信知识</span><b>${trustedCount}</b></div></div>${error}${blindGate}<div class="learning-fields">${fieldsHTML}</div>${empty}</div>`
  }

  function bindLearningPanel(shot) {
    const state = reviewFor(shot.shot_id)
    $('#initialAnalyze')?.addEventListener('click', requestInitialAnalysis)
    $('#revealBlind')?.addEventListener('click', () => {
      state.learning.blindSubmission = Object.fromEntries(LEARNING_FIELDS
        .map((field) => [field, String(state.learning.humanDraft[field] || '').trim()])
        .filter(([, value]) => value))
      state.learning.revealed = true
      state.learning.blindCompleted = true
      state.learning.aiEverVisible = true
      state.updatedAt = new Date().toISOString()
      save()
      renderInspector()
    })
    $$('[data-human-field]').forEach((input) => input.addEventListener('input', (event) => {
      const field = event.target.dataset.humanField
      state.learning.humanDraft[field] = event.target.value
      const button = document.querySelector(`[data-correct-field="${field}"]`)
      if (button) button.disabled = !event.target.value.trim()
      const reveal = $('#revealBlind')
      if (reveal) reveal.disabled = !LEARNING_FIELDS.some((name) => String(state.learning.humanDraft[name] || '').trim())
      state.updatedAt = new Date().toISOString()
      save()
    }))
    $$('[data-reason-field]').forEach((input) => input.addEventListener('change', (event) => {
      const field = event.target.dataset.reasonField
      state.learning.reasons[field] = event.target.value
      if (state.learning.decisions[field]) state.learning.decisions[field].reason = event.target.value
      save()
    }))
    $$('[data-evidence-field]').forEach((input) => input.addEventListener('input', (event) => {
      const field = event.target.dataset.evidenceField
      state.learning.evidence[field] = event.target.value
      if (state.learning.decisions[field]) state.learning.decisions[field].evidence = event.target.value
      save()
    }))
    $$('[data-accept-field]').forEach((button) => button.addEventListener('click', () => decideLearningField(button.dataset.acceptField, 'accepted')))
    $$('[data-correct-field]').forEach((button) => button.addEventListener('click', () => decideLearningField(button.dataset.correctField, 'corrected')))
    $$('[data-promote-field]').forEach((button) => button.addEventListener('click', () => promoteKnowledge(button.dataset.promoteField)))
  }

  function buildReplicationTask(shot) {
    const replication = replicationFor(shot.shot_id)
    return {
      schema_version: '1.0',
      task: 'representative_shot_replication',
      project: { project_id: PROJECT.project_id || '', title: PROJECT.title || APP_NAME },
      shot: {
        shot_id: shot.shot_id,
        title: shot.title,
        frames: { in: shot.source_in_frame, out: shot.source_out_frame },
        time: { in: shot.source_in, out: shot.source_out, duration_sec: shot.duration_sec },
        assets: { start: shot.start_frame, middle: shot.middle_frame, end: shot.end_frame, contact_sheet: shot.dense_contact_sheet, clip: shot.clip || '' },
      },
      target_replacement: replication.target || '',
      production_notes: replication.taskNotes || '',
      constraints: {
        action_beats: shot.timeline_beats || '',
        body_and_force: `${shot.body_part_specifics || ''}\n${shot.force_contact_response || ''}`.trim(),
        camera: `${shot.camera_position || ''}\n${shot.camera_motion || ''}\n${shot.lens_fov || ''}\n${shot.focus_depth || ''}`.trim(),
        vfx_and_environment: `${shot.vfx_layers_timing || ''}\n${shot.interactive_lighting || ''}\n${shot.environment_response || ''}`.trim(),
        continuity_in: shot.previous_shot_result || '',
        continuity_out: `${shot.end_state || ''}\n${shot.next_shot_bridge || ''}`.trim(),
        sound: shot.sound_beats || '',
        uncertainties: shot.uncertainties || '',
      },
      validation: Object.fromEntries(REPLICATION_CRITERIA.map(([key, label]) => [key, { label, score_1_to_5: 0, notes: '' }])),
    }
  }

  function replicationPanelHTML(shot) {
    const replication = replicationFor(shot.shot_id)
    const result = replication.result
    const scoreOptions = (value) => [0, 1, 2, 3, 4, 5].map((number) => `<option value="${number}" ${Number(value || 0) === number ? 'selected' : ''}>${number ? `${number} / 5` : '未评分'}</option>`).join('')
    const scores = REPLICATION_CRITERIA.map(([key, label]) => `<span>${label}</span><select data-replication-score="${key}" aria-label="${label}评分">${scoreOptions(replication.scores[key])}</select>`).join('')
    const preview = replicationObjectUrl ? `<video class="replication-preview" src="${replicationObjectUrl}" controls preload="metadata"></video>` : ''
    const resultMeta = result ? `<p>${escapeHTML(result.name)} · ${(Number(result.size || 0) / 1024 / 1024).toFixed(1)} MB${replication.completedAt ? ' · 已完成评估' : ''}</p>` : '<p>尚未选择复刻结果。</p>'
    return `<div class="replication-panel"><h3>代表镜头复刻验证</h3><p>复刻用于检查拉片结论是否足以指导动作、摄影机、VFX、角色身份和连续性。</p><div class="replication-source"><b>${escapeHTML(shot.shot_id)} · 必须保留的动作因果</b><p>${escapeHTML(shot.force_contact_response || '当前动作因果尚未形成最终结论。')}</p></div><div class="replication-form"><label for="replicationTarget">目标角色与场景替换</label><textarea id="replicationTarget" placeholder="目标角色的身份、能力、惯用侧和场景替换规则…">${escapeHTML(replication.target || '')}</textarea><label for="replicationTaskNotes">本镜制作补充</label><textarea id="replicationTaskNotes" placeholder="只填写目标适配，不要改写原片事实…">${escapeHTML(replication.taskNotes || '')}</textarea><label for="replicationFile">复刻结果</label><div class="replication-file"><input id="replicationFile" type="file" accept="video/*">${resultMeta}${preview}</div><label>分项评分</label><div class="score-grid">${scores}</div><label for="replicationNotes">失败原因与通过依据</label><textarea id="replicationNotes" placeholder="指出偏差发生在哪个动作阶段、摄影机变化或特效时序…">${escapeHTML(replication.notes || '')}</textarea></div><div class="replication-actions"><button type="button" class="btn" id="exportReplicationTask">导出本镜任务</button><button type="button" class="btn primary" id="saveReplication">完成评估</button><button type="button" class="btn" id="promoteReplication">沉淀失败经验</button></div></div>`
  }

  function bindReplicationPanel(shot) {
    const replication = replicationFor(shot.shot_id)
    const saveValue = (key, value) => {
      if (replication[key] !== value) replication.completedAt = null
      replication[key] = value
      save()
    }
    $('#replicationTarget')?.addEventListener('input', (event) => saveValue('target', event.target.value))
    $('#replicationTaskNotes')?.addEventListener('input', (event) => saveValue('taskNotes', event.target.value))
    $('#replicationNotes')?.addEventListener('input', (event) => saveValue('notes', event.target.value))
    $$('[data-replication-score]').forEach((select) => select.addEventListener('change', (event) => {
      replication.completedAt = null
      replication.scores[event.target.dataset.replicationScore] = Number(event.target.value)
      save()
    }))
    $('#replicationFile')?.addEventListener('change', (event) => {
      const file = event.target.files?.[0]
      if (!file) return
      if (replicationObjectUrl) URL.revokeObjectURL(replicationObjectUrl)
      replicationObjectUrl = URL.createObjectURL(file)
      replication.result = { name: file.name, size: file.size, type: file.type, lastModified: file.lastModified }
      replication.completedAt = null
      save()
      renderInspector()
    })
    $('#exportReplicationTask')?.addEventListener('click', () => {
      downloadText(`${shot.shot_id}_复刻任务.json`, JSON.stringify(buildReplicationTask(shot), null, 2), 'application/json;charset=utf-8')
      toast(`${shot.shot_id} 复刻任务已导出`)
    })
    $('#saveReplication')?.addEventListener('click', () => {
      if (!replication.result) { toast('先选择复刻结果'); return }
      if (REPLICATION_CRITERIA.some(([key]) => !Number(replication.scores[key]))) { toast('请完成五项评分'); return }
      if (!String(replication.notes || '').trim()) { toast('请记录失败原因或通过依据'); return }
      replication.completedAt = new Date().toISOString()
      save()
      renderInspector()
      toast('复刻评估已保存')
    })
    $('#promoteReplication')?.addEventListener('click', () => {
      if (!replication.completedAt) { toast('先完成复刻评估'); return }
      const low = REPLICATION_CRITERIA.filter(([key]) => Number(replication.scores[key]) <= 2)
      if (!low.length) { toast('没有低分项；无需沉淀失败案例'); return }
      const duplicate = review.knowledge.cards.some((card) => card.replicationAt === replication.completedAt && card.sourceShotId === shot.shot_id)
      if (duplicate) { toast('这次复刻失败已经沉淀'); return }
      review.knowledge.cards.push({
        id: `K-${shot.shot_id}-replication-${Date.now().toString(36)}`,
        status: 'trusted', field: 'replication_failure', fieldLabel: '复刻失败案例',
        conclusion: `低分项：${low.map(([, label]) => label).join('、')}`,
        aiValue: '', reason: 'replication_failure', reasonLabel: '复刻结果未通过', evidence: replication.notes,
        sourceShotId: shot.shot_id, sourceTitle: shot.title, reviewMode: settings.learningMode,
        replicationAt: replication.completedAt, createdAt: new Date().toISOString(),
      })
      save()
      toast('复刻失败经验已进入可信知识')
    })
  }

  function renderInspector() {
    if (!settings.aiEnabled && inspectorTab === 'mentor') inspectorTab = 'core'
    const shot = selected()
    const state = reviewFor(shot.shot_id)
    $$('.inspect-tab').forEach((button) => button.classList.toggle('on', button.dataset.tab === inspectorTab))
    $('#completion').textContent = `质量 ${qualityScore(shot)} · 审核 ${reviewScore(shot.shot_id)}`
    const content = $('#inspectorContent')
    if (inspectorTab === 'core') {
      content.innerHTML = progressSummary(shot) + diagnosticsHTML(shot) + fieldBlock('事实', '画面与空间', shot.screen_layout, `${shot.analysis_confidence}置信`) + fieldBlock('判断', '整镜动作与因果', shot.literal_visual_description, '人工复核') + fieldBlock('事实', '具体肢体', shot.body_part_specifics) + fieldBlock('事实', '摄影机', `${shot.camera_position} ${shot.camera_motion} ${shot.lens_fov}`) + fieldBlock('事实', 'VFX 与环境反馈', `${shot.vfx_layers_timing} ${shot.interactive_lighting} ${shot.environment_response}`) + fieldBlock('待核', '不确定项', shot.uncertainties)
    } else if (inspectorTab === 'learning') {
      content.innerHTML = learningPanelHTML(shot)
      bindLearningPanel(shot)
    } else if (inspectorTab === 'all') {
      content.innerHTML = `<div class="all-groups">${GROUPS.map((group, index) => `<details class="field-group" ${index < 2 ? 'open' : ''}><summary>${group[0]}</summary>${group[1].map((key) => `<div class="field-row" data-field-row="${key}"><h3>${FIELD_LABELS[key]}</h3><p>${escapeHTML(shot[key])}</p></div>`).join('')}</details>`).join('')}</div>`
    } else if (inspectorTab === 'review') {
      content.innerHTML = `<div class="review-panel">${diagnosticsHTML(shot)}<div class="review-callout"><b>最容易漏写</b>${escapeHTML(shot.often_missed)}</div><div class="check-title">本镜确认项</div>${CHECKS.map((label, index) => `<label class="check"><input type="checkbox" data-check="${index}" ${state.checks[index] ? 'checked' : ''}><span>${label}</span></label>`).join('')}<div class="check-title">人工备注</div><textarea class="notes" id="reviewNotes" placeholder="记录错误、修改建议或生成注意事项…">${escapeHTML(state.notes)}</textarea><div class="save-note">自动保存在当前浏览器；建议定期导出审核文件。</div></div>`
      $$('[data-check]').forEach((input) => input.addEventListener('change', () => { state.checks[Number(input.dataset.check)] = input.checked; state.updatedAt = new Date().toISOString(); save(); renderInspector(); renderList() }))
      $('#reviewNotes').addEventListener('input', (event) => { state.notes = event.target.value; state.updatedAt = new Date().toISOString(); save() })
    } else if (inspectorTab === 'mentor') {
      content.innerHTML = advisorPanelHTML(shot)
      bindAdvisorPanel(shot)
      requestAnimationFrame(scrollAdvisorToBottom)
    } else {
      content.innerHTML = replicationPanelHTML(shot)
      bindReplicationPanel(shot)
    }
    $$('.diagnostic-item').forEach((button) => button.addEventListener('click', () => jumpToField(button.dataset.field)))
  }

  function jumpToField(field) {
    if (!field) return
    inspectorTab = 'all'
    renderInspector()
    requestAnimationFrame(() => {
      const row = document.querySelector(`[data-field-row="${field}"]`)
      const details = row?.closest('details')
      if (details) details.open = true
      row?.scrollIntoView({ block: 'center', behavior: 'smooth' })
    })
  }

  function updateActionButtons(state) {
    const issueButton = $('#issueBtn')
    const confirmButton = $('#confirmBtn')
    issueButton.textContent = state.status === 'issue' ? '取消问题标记' : '标记问题'
    issueButton.classList.toggle('primary', state.status === 'issue')
    confirmButton.textContent = state.status === 'confirmed' ? '已确认 · 看下一镜　→' : '确认并看下一镜　→'
  }

  function setStatus(status, next = false) {
    const shot = selected()
    const state = reviewFor(shot.shot_id)
    state.status = status
    state.updatedAt = new Date().toISOString()
    save()
    renderList()
    renderInspector()
    updateActionButtons(state)
    toast(status === 'confirmed' ? `${shot.shot_id} 已确认` : `${shot.shot_id} 已标记问题`)
    if (next) go(1)
  }

  function go(delta) {
    const current = SHOTS.findIndex((shot) => shot.shot_id === selectedId)
    const next = Math.max(0, Math.min(SHOTS.length - 1, current + delta))
    if (next !== current) selectShot(SHOTS[next].shot_id)
  }

  function goToNext(predicate, message) {
    const start = SHOTS.findIndex((shot) => shot.shot_id === selectedId)
    for (let offset = 1; offset <= SHOTS.length; offset += 1) {
      const candidate = SHOTS[(start + offset) % SHOTS.length]
      if (predicate(candidate)) { selectShot(candidate.shot_id); toast(message); return }
    }
    toast('没有找到符合条件的镜头')
  }

  function downloadText(name, content, type = 'text/plain;charset=utf-8') {
    const blob = new Blob([content], { type })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = name
    link.click()
    setTimeout(() => URL.revokeObjectURL(url), 500)
  }

  function reviewPayload() {
    const metrics = learningMetrics()
    return {
      schema_version: '5.0', exported_at: new Date().toISOString(), source: `${APP_NAME}人机学习审片工作台`,
      project: PROJECT,
      summary: { total: SHOTS.length, confirmed: SHOTS.filter((shot) => statusFor(shot.shot_id) === 'confirmed').length, issues: SHOTS.filter((shot) => statusFor(shot.shot_id) === 'issue').length, attention: SHOTS.filter(hasAttention).length, learning: metrics },
      state: review,
      review: review.shots,
      advisor_history: advisorHistory,
    }
  }

  function exportReviewJSON() {
    downloadText(`拉片学习工程_${PROJECT.project_id || 'local'}_${new Date().toISOString().slice(0, 10)}.json`, JSON.stringify(reviewPayload(), null, 2), 'application/json;charset=utf-8')
    toast('学习工程 JSON 已导出')
  }

  function exportTrustedKnowledge() {
    const cards = review.knowledge.cards.filter((card) => card.status === 'trusted')
    if (!cards.length) { toast('还没有经过人工晋级的可信知识'); return }
    const lines = ['# 拉片超人可信知识库', '', `- 导出时间：${new Date().toLocaleString()}`, `- 项目：${PROJECT.title || APP_NAME}`, `- 知识卡：${cards.length}`, '']
    for (const card of cards) {
      lines.push(`## ${card.fieldLabel || card.field} · ${card.sourceShotId}`, '', `- 来源：${card.sourceTitle || ''}`, `- 审核模式：${card.reviewMode || '未记录'}`, `- 纠错原因：${card.reasonLabel || reasonLabel(card.reason)}`, '', '### 最终结论', '', card.conclusion || '（空）', '', '### 证据', '', card.evidence || '（空）', '')
      if (card.aiValue) lines.push('### AI 原判断', '', card.aiValue, '')
    }
    downloadText(`可信知识库_${new Date().toISOString().slice(0, 10)}.md`, lines.join('\n'), 'text/markdown;charset=utf-8')
    toast(`已导出 ${cards.length} 条可信知识`)
  }

  function exportReplicationRecords() {
    const records = Object.entries(review.replications).filter(([, item]) => item?.completedAt).map(([shotId, item]) => ({ shot_id: shotId, ...item }))
    if (!records.length) { toast('还没有完成的复刻评估'); return }
    downloadText(`复刻评估_${new Date().toISOString().slice(0, 10)}.json`, JSON.stringify({ schema_version: '1.0', project: PROJECT, records }, null, 2), 'application/json;charset=utf-8')
    toast(`已导出 ${records.length} 条复刻评估`)
  }

  async function importReviewBackup(file) {
    try {
      const payload = JSON.parse(await file.text())
      const incomingProject = payload.project || {}
      if (PROJECT.project_id && incomingProject.project_id && PROJECT.project_id !== incomingProject.project_id) throw new Error(`备份属于项目 ${incomingProject.project_id}，当前项目是 ${PROJECT.project_id}`)
      let incoming = payload.state
      if (!incoming && payload.review && typeof payload.review === 'object') incoming = { schemaVersion: 4, shots: payload.review, knowledge: { cards: [] }, replications: {} }
      if (!incoming || typeof incoming !== 'object' || !incoming.shots || typeof incoming.shots !== 'object') throw new Error('没有识别到有效的镜头审核状态')
      const known = new Set(SHOTS.map((shot) => shot.shot_id))
      const unknown = Object.keys(incoming.shots).filter((id) => !known.has(id))
      if (unknown.length) throw new Error(`备份包含当前项目不存在的镜头：${unknown.slice(0, 5).join('、')}`)
      if (!window.confirm(`恢复 ${Object.keys(incoming.shots).length} 个镜头的学习记录？当前浏览器状态将被替换。`)) return
      review = incoming
      review.schemaVersion = 5
      if (!review.knowledge || !Array.isArray(review.knowledge.cards)) review.knowledge = { cards: [] }
      if (!review.replications || typeof review.replications !== 'object') review.replications = {}
      if (payload.advisor_history?.shots) advisorHistory = payload.advisor_history
      SHOTS.forEach((shot) => reviewFor(shot.shot_id))
      writeJSON(STORAGE_KEY, review)
      writeJSON(ADVISOR_STORAGE_KEY, advisorHistory)
      selectedId = known.has(review.lastShot) ? review.lastShot : firstShotId
      location.hash = selectedId
      renderList()
      renderShot()
      updateCounts()
      toast('学习工程已恢复')
    } catch (error) {
      toast(error.message || '无法恢复审核备份')
    }
  }

  function shotMarkdown(shot) {
    const state = reviewFor(shot.shot_id)
    const diagnostics = diagnoseShot(shot)
    const learning = Object.entries(state.learning.decisions).flatMap(([field, decision]) => [`### 人机结论 · ${FIELD_LABELS[field] || field}`, '', `- 结果：${decision.outcome === 'accepted' ? 'AI 判断可用' : '采用人工判断'}`, `- 模式：${decision.mode}`, `- 原因：${reasonLabel(decision.reason)}`, `- 证据：${decision.evidence || '未填写'}`, '', decision.finalValue || '（空）', ''])
    return [`## ${shot.shot_id} · ${shot.title}`, '', `- 帧范围：F${shot.source_in_frame}—F${shot.source_out_frame}`, `- 时间：${shot.source_in}—${shot.source_out}`, `- 状态：${state.status}`, `- 自动质量：${qualityScore(shot)}%`, `- 人工确认：${reviewScore(shot.shot_id)}%`, diagnostics.length ? `- 诊断：${diagnostics.map((item) => item.title).join('；')}` : '- 诊断：无阻断项', '', ...Object.entries(FIELD_LABELS).flatMap(([key, label]) => [`### ${label}`, '', shot[key] || '（空）', '']), ...learning, state.notes ? `### 人工备注\n\n${state.notes}\n` : ''].join('\n')
  }

  function exportMarkdown(issuesOnly = false) {
    const list = SHOTS.map(effectiveShot).filter((shot) => !issuesOnly || hasAttention(rawShot(shot.shot_id)))
    const title = issuesOnly ? '逐帧拉片问题清单' : '完整逐帧拉片审核报告'
    const content = [`# ${title}`, '', `- 导出时间：${new Date().toLocaleString()}`, `- 镜头数量：${list.length} / ${SHOTS.length}`, '', ...list.map(shotMarkdown)].join('\n')
    downloadText(`${title}_${new Date().toISOString().slice(0, 10)}.md`, content, 'text/markdown;charset=utf-8')
    toast(`${title}已导出`)
  }

  function buildShotAiTask(shot) {
    const diagnostics = diagnoseShot(shot)
    const fields = Object.fromEntries(Object.keys(FIELD_LABELS).map((key) => [key, shot[key] || '']))
    return {
      schema_version: '1.0', task: 'single_shot_breakdown_revision',
      identity: { shot_id: shot.shot_id, source_in_frame: shot.source_in_frame, source_out_frame: shot.source_out_frame, source_in: shot.source_in, source_out: shot.source_out },
      assets: { start_frame: shot.start_frame, middle_frame: shot.middle_frame, end_frame: shot.end_frame, dense_contact_sheet: shot.dense_contact_sheet, clip: shot.clip || '' },
      context: { previous_shot_result: shot.previous_shot_result, current_description: shot.literal_visual_description, next_shot_bridge: shot.next_shot_bridge },
      current_fields: fields,
      diagnostics,
      instructions: [
        '只依据帧图、视频和前后镜证据修订，不得凭空补造角色、动作或特效。',
        '区分角色位移与摄影机运动；变焦、推拉、摇移必须明确写入摄影机字段。',
        '动作必须覆盖起因、准备、发力、接触、响应、收势及下一镜交接。',
        'VFX 必须写出现、增强、反转、消散的时机和空间层级。',
        '无法确认的内容写入 uncertainties，不要伪装成事实。',
        `返回 JSON，shot_id 必须保持为 ${shot.shot_id}。`,
      ],
      response_schema: { shot_id: shot.shot_id, patch: Object.fromEntries(EDITABLE_FIELDS.map((key) => [key, `可选：修订后的${FIELD_LABELS[key] || key}`])), confidence: 0.0, rationale: ['修订依据'] },
    }
  }

  function exportCurrentShotAiTask() {
    const shot = selected()
    downloadText(`${shot.shot_id}_AI返修任务.json`, JSON.stringify(buildShotAiTask(shot), null, 2), 'application/json;charset=utf-8')
    toast(`${shot.shot_id} AI 返修任务已导出`)
  }

  function extractJson(text) {
    const trimmed = text.trim()
    const fenced = trimmed.match(/```(?:json)?\s*([\s\S]*?)\s*```/i)
    const source = fenced ? fenced[1].trim() : trimmed
    const start = Math.min(...['{', '['].map((char) => { const index = source.indexOf(char); return index < 0 ? Infinity : index }))
    if (!Number.isFinite(start)) throw new Error('没有找到 JSON 内容')
    return JSON.parse(source.slice(start).replace(/,\s*([}\]])/g, '$1'))
  }

  function normalizeAiResult(value) {
    const shot = selected()
    const source = value?.patch || value?.shot || value?.result?.patch || value?.data?.patch || value
    const shotId = value?.shot_id || value?.identity?.shot_id || value?.result?.shot_id || source?.shot_id
    if (!shotId) throw new Error(`AI 结果缺少 shot_id；为防止串镜，请明确返回 ${shot.shot_id}`)
    if (String(shotId).toUpperCase() !== shot.shot_id) throw new Error(`AI 结果属于 ${shotId}，当前镜头是 ${shot.shot_id}`)
    if (!source || typeof source !== 'object' || Array.isArray(source)) throw new Error('没有识别到可应用的字段对象')
    const patch = {}
    for (const key of EDITABLE_FIELDS) {
      const incoming = source[key]
      if (typeof incoming === 'string' && incoming.trim()) patch[key] = incoming.trim()
    }
    if (!Object.keys(patch).length) throw new Error('AI 结果没有包含可识别的修订字段')
    return patch
  }

  function aiChangeEligible(shot, key, incoming, mode) {
    const current = String(shot[key] || '').trim()
    if (current === incoming) return false
    if (mode === 'fill-empty') return !current
    if (mode === 'append') return !current || !current.includes(incoming)
    return true
  }

  function renderAiPreview() {
    if (!aiPreview) return
    const shot = selected()
    const mode = $('#aiImportMode').value
    const eligible = aiPreview.changes.filter(([key, value]) => aiChangeEligible(shot, key, value, mode))
    const modeLabel = $('#aiImportMode').selectedOptions[0]?.textContent || mode
    $('#aiPreview').className = 'ai-preview ready'
    $('#aiPreview').innerHTML = aiPreview.changes.length ? `<b>识别到 ${aiPreview.changes.length} 项变化；“${escapeHTML(modeLabel)}”可应用 ${eligible.length} 项</b>${aiPreview.changes.map(([key, value]) => { const canApply = aiChangeEligible(shot, key, value, mode); return `<div class="${canApply ? '' : 'skipped'}"><span>${escapeHTML(FIELD_LABELS[key] || key)}</span><p>${escapeHTML(truncate(shot[key], 90))}</p><i>→</i><p>${escapeHTML(truncate(value, 90))}</p></div>` }).join('')}` : '<b>内容与当前镜头一致，没有变化。</b>'
    $('#applyAiImport').disabled = !eligible.length
  }

  function previewAiImport() {
    try {
      const value = extractJson($('#aiImportText').value)
      const patch = normalizeAiResult(value)
      const shot = selected()
      const changes = Object.entries(patch).filter(([key, value]) => String(shot[key] || '').trim() !== value)
      aiPreview = { patch, changes }
      renderAiPreview()
    } catch (error) {
      aiPreview = null
      $('#aiPreview').className = 'ai-preview error'
      $('#aiPreview').textContent = error.message || '无法解析 AI 结果'
      $('#applyAiImport').disabled = true
    }
  }

  function applyAiImport() {
    if (!aiPreview) return
    const mode = $('#aiImportMode').value
    const shot = selected()
    const state = reviewFor(shot.shot_id)
    const nextPatch = { ...(state.patch || {}) }
    if (mode === 'replace' && !window.confirm(`将用 AI 结果替换 ${shot.shot_id} 的对应字段，仍可通过“恢复原始描述”撤回。继续吗？`)) return
    let applied = 0
    for (const [key, incoming] of Object.entries(aiPreview.patch)) {
      const current = String(shot[key] || '').trim()
      if (mode === 'fill-empty') {
        if (!current) { nextPatch[key] = incoming; applied += 1 }
      } else if (mode === 'append') {
        if (!current) { nextPatch[key] = incoming; applied += 1 }
        else if (!current.includes(incoming)) { nextPatch[key] = `${current}\n\n${incoming}`; applied += 1 }
      } else if (current !== incoming) {
        nextPatch[key] = incoming
        applied += 1
      }
    }
    if (!applied) { toast('当前合并模式下没有可应用的变化'); return }
    state.history = [...(state.history || []), { at: new Date().toISOString(), patch: state.patch || {} }].slice(-10)
    state.patch = nextPatch
    state.status = 'pending'
    state.updatedAt = new Date().toISOString()
    save()
    $('#aiImportDialog').close()
    renderList()
    renderShot()
    toast(`${shot.shot_id} 已导入 AI 修订，等待人工确认`)
  }

  function resetCurrentPatch() {
    const state = reviewFor(selectedId)
    if (!Object.keys(state.patch || {}).length) { toast('当前镜头没有 AI 修订'); return }
    if (!window.confirm(`恢复 ${selectedId} 的原始描述？当前 AI 修订会存入最近历史。`)) return
    state.history = [...(state.history || []), { at: new Date().toISOString(), patch: state.patch || {} }].slice(-10)
    state.patch = {}
    state.status = 'pending'
    save()
    renderList()
    renderShot()
    toast('已恢复当前镜头的原始描述')
  }

  function metricIntensity(shot) {
    const text = `${shot.title} ${shot.timeline_beats} ${shot.force_contact_response} ${shot.vfx_layers_timing}`
    const hits = (text.match(/击|踢|踹|撞|爆|冲|飞|摔|重击|光束|能量|破坏|高速|瞬间/g) || []).length
    return Math.max(15, Math.min(100, 20 + hits * 8 + (Number(shot.duration_sec) < 0.5 ? 12 : 0)))
  }

  function metricComplexity(shot) {
    let value = 20
    if (!/^\s*0\s*个?分身/.test(shot.clone_count_and_positions || '')) value += 22
    if (!/(固定|无明显)/.test(shot.camera_motion || '')) value += 14
    if (textPresent(shot.vfx_layers_timing, 45)) value += 16
    if (textPresent(shot.force_contact_response, 70)) value += 12
    if (textPresent(shot.environment_response, 45)) value += 8
    if (Number(shot.duration_sec) < 0.35) value += 8
    return Math.min(100, value)
  }

  function metricRisk(shot) {
    const warnings = diagnoseShot(shot)
    let value = 10 + warnings.reduce((sum, item) => sum + (item.severity === 'high' ? 18 : item.severity === 'medium' ? 9 : 4), 0)
    if (statusFor(shot.shot_id) === 'issue') value += 20
    if (statusFor(shot.shot_id) === 'confirmed') value -= 15
    return Math.max(0, Math.min(100, value))
  }

  function curveSvg() {
    const width = 1200, height = 220, pad = 28
    const series = [
      { name: '战斗强度', color: '#d96537', values: SHOTS.map((shot) => metricIntensity(effectiveShot(shot))) },
      { name: '生成难度', color: '#456f8f', values: SHOTS.map((shot) => metricComplexity(effectiveShot(shot))) },
      { name: '连续性风险', color: '#9c3f4b', values: SHOTS.map((shot) => metricRisk(effectiveShot(shot))) },
    ]
    const points = (values) => values.map((value, index) => `${pad + index / Math.max(1, values.length - 1) * (width - pad * 2)},${height - pad - value / 100 * (height - pad * 2)}`).join(' ')
    return `<div class="curve-legend">${series.map((item) => `<span style="--curve:${item.color}"><i></i>${item.name}</span>`).join('')}<em>启发式曲线，用于发现异常和排序，不替代人工判断。</em></div><svg class="global-curves" viewBox="0 0 ${width} ${height}" role="img" aria-label="战斗强度、生成难度和连续性风险曲线"><g class="curve-grid">${[0, 25, 50, 75, 100].map((value) => `<line x1="${pad}" y1="${height - pad - value / 100 * (height - pad * 2)}" x2="${width - pad}" y2="${height - pad - value / 100 * (height - pad * 2)}"></line><text x="2" y="${height - pad - value / 100 * (height - pad * 2) + 4}">${value}</text>`).join('')}</g>${series.map((item) => `<polyline fill="none" stroke="${item.color}" stroke-width="3" points="${points(item.values)}"></polyline>`).join('')}</svg>`
  }

  function laneText(shot, lane) {
    if (lane === '动作') return truncate(shot.timeline_beats || shot.literal_visual_description, 34)
    if (lane === '摄影机') return truncate(`${shot.camera_motion || ''} ${shot.lens_fov || ''}`, 32)
    if (lane === '接触力学') return truncate(shot.force_contact_response, 34)
    if (lane === 'VFX') return truncate(shot.vfx_layers_timing, 34)
    if (lane === '声音') return truncate(shot.sound_beats, 34)
    return `${diagnoseShot(shot).length} 项 · ${qualityScore(shot)}分`
  }

  function laneLevel(shot, lane) {
    if (lane === '动作') return metricIntensity(shot)
    if (lane === '摄影机') {
      const text = `${shot.camera_motion || ''} ${shot.lens_fov || ''}`
      if (/(变焦|推近|拉远|拉焦|焦段变化)/.test(text)) return 88
      if (/(跟|推|拉|摇|移|环绕|升|降)/.test(text) && !/固定|无明显/.test(text)) return 66
      return 28
    }
    if (lane === '接触力学') {
      const text = shot.force_contact_response || ''
      const cues = (text.match(/接触|命中|受力|反作用|重心|惯性|冲量|方向|弹开|踹飞|撞飞|压制/g) || []).length
      return Math.min(96, 24 + cues * 10 + Math.min(20, Math.round(text.length / 12)))
    }
    if (lane === 'VFX') return metricComplexity(shot)
    if (lane === '声音') {
      const text = shot.sound_beats || ''
      const cues = (text.match(/击|爆|风|呼啸|破空|落地|撞|能量|低频|停顿|静音|重音/g) || []).length
      return Math.min(94, 18 + cues * 9 + Math.min(18, Math.round(text.length / 15)))
    }
    return metricRisk(shot)
  }

  function renderGlobalView() {
    const lanes = ['动作', '摄影机', '接触力学', 'VFX', '声音', '风险']
    const metrics = learningMetrics()
    const header = `<div class="lane-label lane-corner">维度 / 镜头</div>${SHOTS.map((raw) => { const shot = effectiveShot(raw); return `<button class="lane-head" data-global-shot="${shot.shot_id}"><b>${shot.shot_id}</b><span>${Number(shot.duration_sec).toFixed(2)}s</span></button>` }).join('')}`
    const rows = lanes.map((lane) => `<div class="lane-label">${lane}</div>${SHOTS.map((raw) => { const shot = effectiveShot(raw); const level = laneLevel(shot, lane); return `<button class="lane-cell" style="--level:${level}%" data-lane="${lane}" data-global-shot="${shot.shot_id}" title="${escapeHTML(laneText(shot, lane))}"><span>${escapeHTML(laneText(shot, lane))}</span></button>` }).join('')}`).join('')
    $('#globalContent').innerHTML = `<section class="global-section"><h3>全局动作泳道</h3><p>每列对应一个镜头；点击任意单元格返回该镜头。横向滚动用于检查动作、摄影机、力学、VFX 与声音是否连续。</p><div class="lane-scroll"><div class="lane-grid" style="--shot-count:${SHOTS.length}">${header}${rows}</div></div></section><section class="global-section"><h3>战斗生产曲线</h3>${curveSvg()}</section><section class="global-section"><h3>人机学习记录</h3><p>这些数字来自已保存的审核行为，不把自动质量分当成学习结果。</p><div class="learning-global"><div><span>已有 AI 初拉</span><b>${metrics.draftedShots}</b></div><div><span>字段结论</span><b>${metrics.decisions}</b></div><div><span>纠正 AI</span><b>${metrics.corrections}</b></div><div><span>盲审决定</span><b>${metrics.blindDecisions}</b></div><div><span>可信知识</span><b>${metrics.knowledge}</b></div><div><span>复刻评估</span><b>${metrics.replications}</b></div></div></section>`
    $$('[data-global-shot]').forEach((button) => button.addEventListener('click', () => { $('#globalDialog').close(); selectShot(button.dataset.globalShot) }))
  }

  function bindResize(handle, side) {
    let startX, startWidth
    handle.addEventListener('pointerdown', (event) => { startX = event.clientX; startWidth = side === 'left' ? settings.railWidth : settings.inspectorWidth; handle.setPointerCapture(event.pointerId) })
    handle.addEventListener('pointermove', (event) => { if (startX == null) return; const delta = event.clientX - startX; const value = side === 'left' ? startWidth + delta : startWidth - delta; settings[side === 'left' ? 'railWidth' : 'inspectorWidth'] = Math.max(side === 'left' ? 190 : 320, Math.min(side === 'left' ? 320 : 520, value)); applySettings() })
    handle.addEventListener('pointerup', () => { startX = null; writeJSON(SETTINGS_KEY, settings) })
  }

  $$('.filter').forEach((button) => button.addEventListener('click', () => { filter = button.dataset.filter; $$('.filter').forEach((item) => item.classList.toggle('on', item === button)); renderList() }))
  $$('.media-tab').forEach((button) => button.addEventListener('click', () => { mediaMode = button.dataset.media; renderMedia() }))
  $$('.inspect-tab').forEach((button) => button.addEventListener('click', () => { inspectorTab = button.dataset.tab; renderInspector() }))
  $('#search').addEventListener('input', (event) => { query = event.target.value; $('#clearSearch').style.display = query ? 'block' : 'none'; renderList() })
  $('#clearSearch').addEventListener('click', () => { $('#search').value = ''; query = ''; $('#clearSearch').style.display = 'none'; renderList(); $('#search').focus() })
  $('#issueBtn').addEventListener('click', () => setStatus(statusFor(selectedId) === 'issue' ? 'pending' : 'issue'))
  $('#confirmBtn').addEventListener('click', () => setStatus('confirmed', true))
  $('#nextAttentionBtn').addEventListener('click', () => goToNext((shot) => hasAttention(shot), '已跳到下一诊断镜头'))
  $('#nextPendingBtn').addEventListener('click', () => goToNext((shot) => statusFor(shot.shot_id) !== 'confirmed', '已跳到下一未确认镜头'))
  $('#exportBtn').addEventListener('click', () => $('#exportDialog').showModal())
  $('#globalViewBtn').addEventListener('click', () => { renderGlobalView(); $('#globalDialog').showModal() })
  $('#shotAiBtn').addEventListener('click', () => {
    if (!settings.aiEnabled) { toast('AI 是可选功能，可在“舒适度”中启用'); return }
    inspectorTab = 'mentor'
    if (window.matchMedia('(max-width: 920px)').matches) {
      $('#inspector').classList.add('mobile-open')
      $('#mobileInspectBtn').textContent = '关闭'
    }
    renderInspector()
  })
  $('#theaterBtn').addEventListener('click', () => { document.body.classList.toggle('theater'); $('#theaterBtn').textContent = document.body.classList.contains('theater') ? '退出影院' : '影院' })
  $('#mobileInspectBtn').addEventListener('click', () => { const open = $('#inspector').classList.toggle('mobile-open'); $('#mobileInspectBtn').textContent = open ? '关闭' : '审核' })
  $('#playbackRate').addEventListener('click', () => { const rates = [0.25, 0.5, 1], current = Number($('#playbackRate').textContent.replace('×', '')), next = rates[(rates.indexOf(current) + 1) % rates.length]; $('#playbackRate').textContent = `${next}×`; const video = $('#reviewVideo'); if (video) video.playbackRate = next })
  $('#muteBtn').addEventListener('click', () => { const video = $('#reviewVideo'), muted = $('#muteBtn').textContent === '原音'; $('#muteBtn').textContent = muted ? '静音' : '原音'; if (video) video.muted = muted })

  const comfortDialog = $('#comfortDialog')
  $('#comfortBtn').addEventListener('click', () => comfortDialog.showModal())
  $('#fontSize').addEventListener('change', (event) => { settings.fontSize = Number(event.target.value); applySettings(); writeJSON(SETTINGS_KEY, settings) })
  $('#density').addEventListener('change', (event) => { settings.density = event.target.value; applySettings(); writeJSON(SETTINGS_KEY, settings) })
  $('#viewTone').addEventListener('change', (event) => { settings.viewTone = event.target.value; applySettings(); writeJSON(SETTINGS_KEY, settings) })
  $('#aiEnabled').addEventListener('change', (event) => {
    settings.aiEnabled = event.target.value === 'true'
    if (!settings.aiEnabled) advisorRuntime.controller?.abort()
    applySettings()
    writeJSON(SETTINGS_KEY, settings)
    renderInspector()
    toast(settings.aiEnabled ? 'AI 影像导师已启用；普通审片仍可独立使用' : 'AI 影像导师已关闭，不会发起 AI 网络请求')
  })
  $('#learningMode').addEventListener('change', (event) => {
    settings.learningMode = event.target.value
    const learning = reviewFor(selectedId).learning
    if (settings.learningMode === 'blind') {
      learning.blindCompleted = false
      learning.blindSessionValid = !learning.aiEverVisible
      learning.blindSubmission = {}
      learning.revealed = false
    } else {
      learning.revealed = true
      if (Object.keys(learning.aiDraft).length) learning.aiEverVisible = true
    }
    applySettings()
    writeJSON(SETTINGS_KEY, settings)
    renderInspector()
    toast(({ learn: '已切换到学习模式', blind: '已切换到盲审模式', efficient: '已切换到效率模式' })[settings.learningMode])
  })
  $('#railWidth').addEventListener('input', (event) => { settings.railWidth = Number(event.target.value); applySettings(); writeJSON(SETTINGS_KEY, settings) })
  $('#inspectorWidth').addEventListener('input', (event) => { settings.inspectorWidth = Number(event.target.value); applySettings(); writeJSON(SETTINGS_KEY, settings) })

  $$('.dialog-close-action').forEach((button) => button.addEventListener('click', () => button.closest('dialog').close()))
  $$('dialog').forEach((dialog) => dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close() }))
  $('#exportReviewJson').addEventListener('click', exportReviewJSON)
  $('#exportFullMarkdown').addEventListener('click', () => exportMarkdown(false))
  $('#exportIssuesMarkdown').addEventListener('click', () => exportMarkdown(true))
  $('#exportShotAiTask').addEventListener('click', exportCurrentShotAiTask)
  $('#exportKnowledge').addEventListener('click', exportTrustedKnowledge)
  $('#exportReplication').addEventListener('click', exportReplicationRecords)
  $('#reviewBackupFile').addEventListener('change', async (event) => { const file = event.target.files?.[0]; if (file) await importReviewBackup(file); event.target.value = '' })
  $('#openAiImport').addEventListener('click', () => { $('#exportDialog').close(); $('#aiImportDialog').showModal(); $('#aiImportText').focus() })
  $('#aiImportFile').addEventListener('change', async (event) => { const file = event.target.files?.[0]; if (!file) return; $('#aiImportText').value = await file.text(); previewAiImport() })
  $('#aiImportText').addEventListener('input', () => { aiPreview = null; $('#aiPreview').className = 'ai-preview'; $('#aiPreview').textContent = '内容已变化，请重新预览并校验镜头编号。'; $('#applyAiImport').disabled = true })
  $('#aiImportMode').addEventListener('change', renderAiPreview)
  $('#previewAiImport').addEventListener('click', previewAiImport)
  $('#applyAiImport').addEventListener('click', applyAiImport)
  $('#resetCurrentPatch').addEventListener('click', resetCurrentPatch)

  bindResize($('#leftResize'), 'left')
  bindResize($('#rightResize'), 'right')
  document.addEventListener('keydown', (event) => {
    const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName)
    if (event.key === '/' && !typing) { event.preventDefault(); $('#search').focus(); return }
    if (typing) return
    if (['j', 'J', 'ArrowDown'].includes(event.key)) { event.preventDefault(); go(1) }
    else if (['k', 'K', 'ArrowUp'].includes(event.key)) { event.preventDefault(); go(-1) }
    else if (event.code === 'Space') { const video = $('#reviewVideo'); if (video) { event.preventDefault(); video.paused ? video.play() : video.pause() } }
    else if (['f', 'F'].includes(event.key)) { event.preventDefault(); $('#issueBtn').click() }
    else if (['c', 'C'].includes(event.key)) { event.preventDefault(); $('#confirmBtn').click() }
    else if (['t', 'T'].includes(event.key)) { event.preventDefault(); $('#theaterBtn').click() }
    else if (['g', 'G'].includes(event.key)) { event.preventDefault(); $('#globalViewBtn').click() }
    else if (['a', 'A'].includes(event.key)) { event.preventDefault(); $('#shotAiBtn').click() }
  })
  window.addEventListener('hashchange', () => { const id = location.hash.slice(1).toUpperCase(); if (id !== selectedId && SHOTS.some((shot) => shot.shot_id === id)) selectShot(id, { preserveScroll: true }) })

  const projectTitle = PROJECT.title || '参考视频'
  $('#projectName').textContent = `${projectTitle}　/　人机拉片工程`
  $('#projectName').title = projectTitle
  document.title = `${APP_NAME} · ${projectTitle} · ${SHOTS.length} 镜`
  applySettings()
  updateCounts()
  updateSavedIndicator()
  renderList()
  renderShot()
})()
