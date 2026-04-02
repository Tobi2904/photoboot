import { useState, useRef, useEffect, useCallback, useMemo } from 'react'
import './App.css'
import {
  API_BASE,
  ApiError,
  cancelSession,
  capturePhoto,
  getLiveviewUrl,
  listFrames,
  processSession,
  saveFallbackContact,
  startSession,
  toAbsoluteUrl,
} from './api/client'

const COUNTDOWN_OPTIONS = [0, 3, 5, 10]
const CAPTURE_TARGET = 5
const VN_PHONE_REGEX = /^0(3|5|7|8|9)\d{8}$/
const BACKGROUND_MIN_COUNT = 20
const BACKGROUND_MAX_COUNT = 30
const elementAssetModules = import.meta.glob('./assets/elements/*.{png,jpg,jpeg,webp,svg}', {
  eager: true,
  import: 'default',
})
const elementAssetUrls = Object.values(elementAssetModules)

const createEmptySlots = (frame) => Array(frame?.slots?.length || 0).fill(null)

const normalizeFrame = (frame) => ({
  id: frame.id,
  name: frame.name || frame.id,
  preview: toAbsoluteUrl(frame.preview_url),
  template: toAbsoluteUrl(frame.preview_url),
  slots: Array.isArray(frame.slots)
    ? frame.slots.map((slot) => ({
        x: slot.x,
        y: slot.y,
        width: slot.width,
        height: slot.height,
      }))
    : [],
  outputWidth: frame.output_width,
  outputHeight: frame.output_height,
})

const getErrorMessage = (error) => {
  if (!(error instanceof ApiError)) {
    return 'Đã xảy ra lỗi không xác định. Vui lòng thử lại.'
  }

  if (error.status === 503) {
    return 'Camera Sony đang offline hoặc bận. Vui lòng kiểm tra kết nối USB và thử lại.'
  }
  if (error.status === 502) {
    return 'Upload ảnh lên S3 thất bại. Vui lòng thử lại sau ít giây.'
  }
  if (error.status === 400) {
    return error.message || 'Yêu cầu không hợp lệ. Vui lòng kiểm tra thao tác.'
  }
  if (error.status === 404) {
    return error.message || 'Không tìm thấy tài nguyên.'
  }
  if (error.status === 408) {
    return 'Yêu cầu bị timeout. Vui lòng thử lại.'
  }
  if (error.status === 0) {
    return 'Không kết nối được backend. Kiểm tra server tại ' + API_BASE
  }
  return error.message || 'Đã xảy ra lỗi hệ thống.'
}

const shouldShowFallbackForm = (error) => {
  if (!(error instanceof ApiError)) {
    return false
  }

  if ([0, 408, 502].includes(error.status)) {
    return true
  }

  const message = String(error.message || '').toLowerCase()
  return message.includes('timeout') || message.includes('quá thời gian')
}

const getFallbackReason = (error) => {
  if (!(error instanceof ApiError)) {
    return 'unknown_error'
  }

  if (error.status === 408) {
    return 'timeout'
  }
  if (error.status === 0) {
    return 'network_error'
  }
  if (error.status === 502) {
    return 'upload_fail'
  }
  return 'process_error'
}

const validateVietnamPhone = (phoneValue) => VN_PHONE_REGEX.test(phoneValue)

function App() {
  const [currentStep, setCurrentStep] = useState('select-frame')
  const [frames, setFrames] = useState([])
  const [selectedFrame, setSelectedFrame] = useState(null)
  const [sessionId, setSessionId] = useState(null)

  const [capturedPhotos, setCapturedPhotos] = useState([])
  const [arrangedPhotos, setArrangedPhotos] = useState([])
  const [finalResult, setFinalResult] = useState(null)

  const [selectedCountdownTime, setSelectedCountdownTime] = useState(3)
  const [countdown, setCountdown] = useState(null)
  const [isCapturing, setIsCapturing] = useState(false)

  const [activeSlot, setActiveSlot] = useState(0)
  const [draggedPhoto, setDraggedPhoto] = useState(null)
  const [dragOverSlot, setDragOverSlot] = useState(null)

  const [isLoadingFrames, setIsLoadingFrames] = useState(false)
  const [actionLoading, setActionLoading] = useState({
    startSession: false,
    capture: false,
    process: false,
    cancel: false,
  })
  const [errorMessage, setErrorMessage] = useState('')
  const [showFallbackForm, setShowFallbackForm] = useState(false)
  const [fallbackPhone, setFallbackPhone] = useState('')
  const [fallbackPhoneError, setFallbackPhoneError] = useState('')
  const [fallbackSubmitMessage, setFallbackSubmitMessage] = useState('')
  const [fallbackSubmitting, setFallbackSubmitting] = useState(false)
  const [fallbackErrorReason, setFallbackErrorReason] = useState('')
  const [fallbackSaved, setFallbackSaved] = useState(false)
  const [showLocalPrintReady, setShowLocalPrintReady] = useState(false)
  const [fallbackPreviewUrl, setFallbackPreviewUrl] = useState('')
  const [liveviewReady, setLiveviewReady] = useState(false)
  const [liveviewError, setLiveviewError] = useState(false)
  const [liveviewRetryKey, setLiveviewRetryKey] = useState(0)

  const countdownIntervalRef = useRef(null)
  const sessionIdRef = useRef(sessionId)
  const touchRef = useRef({ photoFilename: null })
  const liveviewRetryTimeoutRef = useRef(null)

  useEffect(() => {
    sessionIdRef.current = sessionId
  }, [sessionId])

  const clearLiveviewRetry = useCallback(() => {
    if (liveviewRetryTimeoutRef.current) {
      clearTimeout(liveviewRetryTimeoutRef.current)
      liveviewRetryTimeoutRef.current = null
    }
  }, [])

  useEffect(() => {
    if (currentStep !== 'capture') {
      clearLiveviewRetry()
      setLiveviewReady(false)
      setLiveviewError(false)
      return
    }

    setLiveviewReady(false)
    setLiveviewError(false)
    setLiveviewRetryKey((prev) => prev + 1)
  }, [currentStep, clearLiveviewRetry])

  useEffect(() => () => clearLiveviewRetry(), [clearLiveviewRetry])

  const scheduleLiveviewRetry = useCallback(() => {
    clearLiveviewRetry()
    liveviewRetryTimeoutRef.current = setTimeout(() => {
      setLiveviewError(false)
      setLiveviewReady(false)
      setLiveviewRetryKey((prev) => prev + 1)
    }, 2000)
  }, [clearLiveviewRetry])

  const handleLiveviewLoad = useCallback(() => {
    clearLiveviewRetry()
    setLiveviewError(false)
    setLiveviewReady(true)
  }, [clearLiveviewRetry])

  const handleLiveviewError = useCallback(() => {
    setLiveviewError(true)
    setLiveviewReady(false)
    scheduleLiveviewRetry()
  }, [scheduleLiveviewRetry])

  const liveviewSrc = useMemo(
    () => getLiveviewUrl(`${sessionId || 'pending'}-${liveviewRetryKey}`),
    [sessionId, liveviewRetryKey],
  )

  const clearCountdown = useCallback(() => {
    if (countdownIntervalRef.current) {
      clearInterval(countdownIntervalRef.current)
      countdownIntervalRef.current = null
    }
    setCountdown(null)
    setIsCapturing(false)
  }, [])

  const loadFrameList = useCallback(async () => {
    setIsLoadingFrames(true)
    setErrorMessage('')
    try {
      const response = await listFrames()
      const normalizedFrames = Array.isArray(response.frames)
        ? response.frames.map(normalizeFrame)
        : []

      setFrames(normalizedFrames)
      setSelectedFrame(normalizedFrames[0] || null)
      setArrangedPhotos(createEmptySlots(normalizedFrames[0]))
    } catch (error) {
      setErrorMessage(getErrorMessage(error))
    } finally {
      setIsLoadingFrames(false)
    }
  }, [])

  useEffect(() => {
    void loadFrameList()
  }, [loadFrameList])

  const startNewSession = useCallback(async () => {
    setActionLoading((prev) => ({ ...prev, startSession: true }))
    setErrorMessage('')

    try {
      const response = await startSession()
      setSessionId(response.session_id)
      return true
    } catch (error) {
      setErrorMessage(getErrorMessage(error))
      return false
    } finally {
      setActionLoading((prev) => ({ ...prev, startSession: false }))
    }
  }, [])

  const cancelActiveSession = useCallback(async (shouldShowError = true) => {
    if (!sessionIdRef.current) {
      return
    }

    setActionLoading((prev) => ({ ...prev, cancel: true }))
    try {
      await cancelSession()
      setSessionId(null)
    } catch (error) {
      if (shouldShowError) {
        setErrorMessage(getErrorMessage(error))
      }
    } finally {
      setActionLoading((prev) => ({ ...prev, cancel: false }))
    }
  }, [])

  useEffect(() => {
    return () => {
      clearCountdown()
      if (sessionIdRef.current) {
        cancelSession().catch(() => {})
      }
    }
  }, [clearCountdown])

  const getPhotoByFilename = useCallback(
    (filename) => capturedPhotos.find((photo) => photo.filename === filename),
    [capturedPhotos],
  )

  const executeCapture = useCallback(async () => {
    if (!sessionIdRef.current || actionLoading.capture || actionLoading.process) {
      setIsCapturing(false)
      return
    }

    setActionLoading((prev) => ({ ...prev, capture: true }))
    setErrorMessage('')

    try {
      const response = await capturePhoto()
      const newPhoto = {
        filename: response.filename,
        previewUrl: toAbsoluteUrl(response.preview_url),
      }

      setCapturedPhotos((prev) => {
        if (prev.some((photo) => photo.filename === newPhoto.filename)) {
          return prev
        }
        return [...prev, newPhoto]
      })
    } catch (error) {
      setErrorMessage(getErrorMessage(error))
    } finally {
      setActionLoading((prev) => ({ ...prev, capture: false }))
      setIsCapturing(false)
      setCountdown(null)
    }
  }, [actionLoading.capture, actionLoading.process])

  const startCountdown = useCallback(() => {
    if (isCapturing || actionLoading.capture || !selectedFrame) {
      return
    }

    const maxPhotos = CAPTURE_TARGET
    if (capturedPhotos.length >= maxPhotos) {
      setErrorMessage(`Đã chụp đủ ${maxPhotos} ảnh cho khung này.`)
      return
    }

    clearCountdown()
    setErrorMessage('')
    setIsCapturing(true)

    if (selectedCountdownTime === 0) {
      void executeCapture()
      return
    }

    let timeLeft = selectedCountdownTime
    setCountdown(timeLeft)

    countdownIntervalRef.current = setInterval(() => {
      timeLeft -= 1
      if (timeLeft <= 0) {
        clearCountdown()
        void executeCapture()
      } else {
        setCountdown(timeLeft)
      }
    }, 1000)
  }, [
    actionLoading.capture,
    capturedPhotos.length,
    clearCountdown,
    executeCapture,
    isCapturing,
    selectedCountdownTime,
    selectedFrame,
  ])

  const autoArrangePhotos = useCallback(() => {
    if (!selectedFrame) return

    const slotCount = selectedFrame.slots.length
    const autoArranged = Array(slotCount).fill(null)

    for (let i = 0; i < slotCount && i < capturedPhotos.length; i += 1) {
      autoArranged[i] = capturedPhotos[i].filename
    }

    setArrangedPhotos(autoArranged)
  }, [capturedPhotos, selectedFrame])

  const goToCapture = async () => {
    if (!selectedFrame) {
      return
    }

    clearCountdown()
    setCapturedPhotos([])
    setArrangedPhotos(createEmptySlots(selectedFrame))
    setActiveSlot(0)
    setFinalResult(null)
    setShowFallbackForm(false)
    setFallbackPhone('')
    setFallbackPhoneError('')
    setFallbackSubmitMessage('')
    setFallbackErrorReason('')
    setFallbackSaved(false)
    setShowLocalPrintReady(false)
    setFallbackPreviewUrl('')

    const started = await startNewSession()
    if (started) {
      setCurrentStep('capture')
    }
  }

  const goToArrange = () => {
    if (!selectedFrame || capturedPhotos.length < CAPTURE_TARGET) {
      return
    }

    autoArrangePhotos()
    setCurrentStep('arrange')
  }

  const goBackToFrameSelect = async () => {
    clearCountdown()
    setCurrentStep('select-frame')
    setCapturedPhotos([])
    setArrangedPhotos(createEmptySlots(selectedFrame))
    setFinalResult(null)
    setActiveSlot(0)
    setShowFallbackForm(false)
    setFallbackPhone('')
    setFallbackPhoneError('')
    setFallbackSubmitMessage('')
    setFallbackErrorReason('')
    setFallbackSaved(false)
    setShowLocalPrintReady(false)
    setFallbackPreviewUrl('')
    await cancelActiveSession(false)
  }

  const continueToLocalPrint = () => {
    setShowFallbackForm(false)
    setFallbackSubmitMessage('')
    setFallbackPhoneError('')
    setShowLocalPrintReady(true)
  }

  const submitFallbackContact = async () => {
    const normalizedPhone = fallbackPhone.trim()
    if (!validateVietnamPhone(normalizedPhone)) {
      setFallbackPhoneError('Vui lòng nhập số di động Việt Nam dạng 0xxxxxxxxx.')
      return
    }

    if (!sessionIdRef.current || !selectedFrame) {
      setFallbackPhoneError('Không tìm thấy session hiện tại. Vui lòng thử lại thao tác.')
      return
    }

    setFallbackSubmitting(true)
    setFallbackPhoneError('')
    setFallbackSubmitMessage('')

    try {
      await saveFallbackContact({
        session_id: sessionIdRef.current,
        phone: normalizedPhone,
        error_reason: fallbackErrorReason || 'process_error',
        frame_template_id: selectedFrame.id,
        selected_photos: arrangedPhotos,
      })
      setFallbackSubmitMessage('Đã lưu thông tin. Nhân viên sẽ liên hệ gửi ảnh qua Zalo thủ công.')
      setFallbackPhone('')
      setFallbackSaved(true)
      setShowFallbackForm(false)
    } catch (error) {
      setFallbackPhoneError(getErrorMessage(error))
    } finally {
      setFallbackSubmitting(false)
    }
  }

  const handlePhotoClick = (photoFilename) => {
    const selectedIndex = arrangedPhotos.findIndex((filename) => filename === photoFilename)

    // Toggle off when clicking a selected photo.
    if (selectedIndex !== -1) {
      const nextArrangement = [...arrangedPhotos]
      nextArrangement[selectedIndex] = null
      setArrangedPhotos(nextArrangement)
      setActiveSlot(selectedIndex)
      setErrorMessage('')
      return
    }

    // Add to first empty slot when clicking an unselected photo.
    const emptyIndex = arrangedPhotos.findIndex((filename) => filename === null)
    if (emptyIndex === -1) {
      setErrorMessage('Ô hình đã đầy. Vui lòng bỏ chọn một hình trong khung để thêm ảnh mới.')
      return
    }

    const nextArrangement = [...arrangedPhotos]
    nextArrangement[emptyIndex] = photoFilename
    setArrangedPhotos(nextArrangement)

    const nextEmpty = nextArrangement.findIndex((filename) => filename === null)
    setActiveSlot(nextEmpty === -1 ? emptyIndex : nextEmpty)
    setErrorMessage('')
  }

  const handleDragStart = (event, photoFilename) => {
    setDraggedPhoto(photoFilename)
    event.dataTransfer.effectAllowed = 'move'
  }

  const handleDragEnd = () => {
    setDraggedPhoto(null)
    setDragOverSlot(null)
  }

  const handleDragOver = (event, slotIndex) => {
    event.preventDefault()
    event.dataTransfer.dropEffect = 'move'
    setDragOverSlot(slotIndex)
  }

  const handleDrop = (event, slotIndex) => {
    event.preventDefault()
    if (!draggedPhoto) return

    setArrangedPhotos((prev) => {
      const next = [...prev]
      const oldSlot = next.indexOf(draggedPhoto)
      if (oldSlot !== -1) {
        next[oldSlot] = null
      }
      next[slotIndex] = draggedPhoto
      return next
    })

    setDraggedPhoto(null)
    setDragOverSlot(null)
  }

  const handleDragLeave = () => {
    setDragOverSlot(null)
  }

  const handleTouchStart = (event, photoFilename) => {
    touchRef.current = { photoFilename }
    setDraggedPhoto(photoFilename)

    if (event.cancelable) {
      event.preventDefault()
    }
  }

  const handleTouchMove = (event) => {
    if (!touchRef.current.photoFilename) return
    if (event.cancelable) {
      event.preventDefault()
    }

    const touch = event.touches[0]
    const elements = document.elementsFromPoint(touch.clientX, touch.clientY)
    const slotElement = elements.find((el) => el.classList.contains('slot'))

    if (!slotElement) {
      setDragOverSlot(null)
      return
    }

    const slotIndex = Number.parseInt(slotElement.dataset.slotIndex || '', 10)
    if (!Number.isNaN(slotIndex)) {
      setDragOverSlot(slotIndex)
    }
  }

  const handleTouchEnd = () => {
    if (touchRef.current.photoFilename && dragOverSlot !== null) {
      const photoFilename = touchRef.current.photoFilename
      setArrangedPhotos((prev) => {
        const next = [...prev]
        const oldSlot = next.indexOf(photoFilename)
        if (oldSlot !== -1) {
          next[oldSlot] = null
        }
        next[dragOverSlot] = photoFilename
        return next
      })
    }

    touchRef.current = { photoFilename: null }
    setDraggedPhoto(null)
    setDragOverSlot(null)
  }

  const handleSlotClick = (slotIndex) => {
    if (arrangedPhotos[slotIndex]) {
      removeFromSlot(slotIndex)
    }
    setActiveSlot(slotIndex)
  }

  const removeFromSlot = (slotIndex) => {
    setArrangedPhotos((prev) => {
      const next = [...prev]
      next[slotIndex] = null
      return next
    })
  }

  const generateFinalImage = async () => {
    if (!selectedFrame || arrangedPhotos.some((filename) => filename === null)) {
      return
    }

    setActionLoading((prev) => ({ ...prev, process: true }))
    setErrorMessage('')
    setFallbackSubmitMessage('')
    setFallbackPhoneError('')
    setFallbackSaved(false)
    setShowLocalPrintReady(false)
    setFallbackPreviewUrl('')

    try {
      const response = await processSession({
        selected_photos: arrangedPhotos,
        frame_template_id: selectedFrame.id,
      })

      setFinalResult({
        finalImageUrl: toAbsoluteUrl(response.final_image_url),
        qrCodeBase64: response.qr_code_base64,
        s3Url: response.s3_url,
      })
      setShowFallbackForm(false)
      setFallbackPhone('')
      setFallbackPhoneError('')
      setFallbackSubmitMessage('')
      setFallbackErrorReason('')
      setFallbackSaved(false)
      setShowLocalPrintReady(false)
      setFallbackPreviewUrl('')
      setSessionId(null)
      setCurrentStep('done')
    } catch (error) {
      if (shouldShowFallbackForm(error)) {
        setFallbackErrorReason(getFallbackReason(error))
        setFallbackPhone('')
        setFallbackPhoneError('')
        setFallbackSubmitMessage('')
        setShowLocalPrintReady(false)
        if (sessionIdRef.current) {
          setFallbackPreviewUrl(`/static/final_outputs/final_${sessionIdRef.current}.jpg`)
        }
        setShowFallbackForm(true)
        setCurrentStep('done')
        setFinalResult(null)
        setErrorMessage('')
      } else {
        setErrorMessage(getErrorMessage(error))
      }
    } finally {
      setActionLoading((prev) => ({ ...prev, process: false }))
    }
  }

  const slotCount = selectedFrame?.slots.length || 0
  const isCaptureFull = capturedPhotos.length >= CAPTURE_TARGET
  const canGoNext = capturedPhotos.length >= CAPTURE_TARGET
  const resolvedFinalImageUrl = finalResult?.finalImageUrl || toAbsoluteUrl(fallbackPreviewUrl)
  const hasQr = Boolean(finalResult?.qrCodeBase64)

  const randomBackgroundElements = useMemo(() => {
    if (!elementAssetUrls.length) {
      return []
    }

    const viewportWidth =
      typeof window !== 'undefined' ? Math.max(window.innerWidth, 960) : 1920
    const viewportHeight =
      typeof window !== 'undefined' ? Math.max(window.innerHeight, 540) : 1080
    const requiredCount = elementAssetUrls.length
    const minCount = Math.max(requiredCount, BACKGROUND_MIN_COUNT)
    const maxCount = Math.max(minCount, BACKGROUND_MAX_COUNT)
    const totalElements =
      minCount + Math.floor(Math.random() * (maxCount - minCount + 1))

    const shuffle = (arr) => {
      const copy = [...arr]
      for (let i = copy.length - 1; i > 0; i -= 1) {
        const j = Math.floor(Math.random() * (i + 1))
        ;[copy[i], copy[j]] = [copy[j], copy[i]]
      }
      return copy
    }

    const sourceQueue = []
    sourceQueue.push(...shuffle(elementAssetUrls))
    while (sourceQueue.length < totalElements) {
      sourceQueue.push(...shuffle(elementAssetUrls))
    }
    const assignedSources = sourceQueue.slice(0, totalElements)

    const ratio = viewportWidth / viewportHeight
    const spreadFactor = 1.45
    const gridCellCount = Math.max(totalElements, Math.ceil(totalElements * spreadFactor))
    const cols = Math.max(1, Math.ceil(Math.sqrt(gridCellCount * ratio)))
    const rows = Math.max(1, Math.ceil(gridCellCount / cols))
    const cellWidth = viewportWidth / cols
    const cellHeight = viewportHeight / rows
    const jitterX = Math.max(14, cellWidth * 0.26)
    const jitterY = Math.max(14, cellHeight * 0.26)

    const cellIndexes = Array.from({ length: cols * rows }, (_, index) => index)
    const sampledCells = shuffle(cellIndexes).slice(0, totalElements)

    return assignedSources.map((src, index) => {
      const cellIndex = sampledCells[index]
      const col = cellIndex % cols
      const row = Math.floor(cellIndex / cols)

      const baseCenterX = col * cellWidth + cellWidth / 2
      const baseCenterY = row * cellHeight + cellHeight / 2
      const centerX = baseCenterX + (Math.random() * 2 - 1) * jitterX
      const centerY = baseCenterY + (Math.random() * 2 - 1) * jitterY

      const minCellSize = Math.min(cellWidth, cellHeight)
      const minSize = Math.max(74, minCellSize * 0.3)
      const maxSize = Math.max(minSize + 28, minCellSize * 0.88)
      const size = Math.round(minSize + Math.random() * (maxSize - minSize))

      const left = Math.max(0, Math.min(viewportWidth - size, centerX - size / 2))
      const top = Math.max(0, Math.min(viewportHeight - size, centerY - size / 2))
      const moveX = (18 + Math.random() * 34) * (Math.random() < 0.5 ? -1 : 1)
      const moveY = (12 + Math.random() * 30) * (Math.random() < 0.5 ? -1 : 1)

      return {
        id: `bg-element-${index}`,
        src,
        top,
        left,
        size,
        rotate: Math.floor(Math.random() * 360),
        opacity: Number((0.22 + Math.random() * 0.3).toFixed(2)),
        moveX: Number(moveX.toFixed(1)),
        moveY: Number(moveY.toFixed(1)),
        moveXBack: Number((-moveX * (0.45 + Math.random() * 0.3)).toFixed(1)),
        moveYBack: Number((-moveY * (0.45 + Math.random() * 0.3)).toFixed(1)),
        duration: Number((7 + Math.random() * 8).toFixed(2)),
        delay: Number((Math.random() * 4).toFixed(2)),
      }
    })
  }, [currentStep])

  return (
    <div className="app">
      {errorMessage && <div className="status-banner error">{errorMessage}</div>}

      {currentStep === 'select-frame' && (
        <div className="fullscreen-step frame-selection">
          <div className="background-elements" aria-hidden="true">
            {randomBackgroundElements.map((element) => (
              <img
                key={element.id}
                src={element.src}
                alt=""
                className="background-element"
                style={{
                  top: `${element.top}px`,
                  left: `${element.left}px`,
                  width: `${element.size}px`,
                  height: `${element.size}px`,
                  opacity: element.opacity,
                  '--base-rotate': `${element.rotate}deg`,
                  '--float-x': `${element.moveX}px`,
                  '--float-y': `${element.moveY}px`,
                  '--float-back-x': `${element.moveXBack}px`,
                  '--float-back-y': `${element.moveYBack}px`,
                  '--float-duration': `${element.duration}s`,
                  '--float-delay': `${element.delay}s`,
                }}
              />
            ))}
          </div>
          <div className="center-content">
            <h1 className="main-title">PHOTOBOOTH</h1>
            <p className="subtitle">Chọn khung ảnh để bắt đầu</p>

            {isLoadingFrames ? (
              <div className="liveview-loading">
                <div className="spinner"></div>
                <p>Đang tải danh sách frame...</p>
              </div>
            ) : (
              <div className="frame-grid">
                {frames.map((frame) => (
                  <button
                    key={frame.id}
                    type="button"
                    className={`frame-card ${selectedFrame?.id === frame.id ? 'selected' : ''}`}
                    onClick={() => {
                      setSelectedFrame(frame)
                      setArrangedPhotos(createEmptySlots(frame))
                    }}
                  >
                    <img src={frame.preview} alt={frame.name} />
                    <span>{frame.name}</span>
                  </button>
                ))}
              </div>
            )}

            <button
              type="button"
              className="btn-primary"
              disabled={!selectedFrame || isLoadingFrames || actionLoading.startSession}
              onClick={goToCapture}
            >
              {actionLoading.startSession ? 'ĐANG TẠO PHIÊN...' : 'BẮT ĐẦU CHỤP ->'}
            </button>
          </div>
        </div>
      )}

      {currentStep === 'capture' && (
        <div className="fullscreen-step capture-step">
          <div className="liveview-fullscreen">
            <img
              src={liveviewSrc}
              alt="Sony a6400 liveview"
              className={`liveview-stream ${liveviewReady && !liveviewError ? 'visible' : ''}`}
              onLoad={handleLiveviewLoad}
              onError={handleLiveviewError}
            />

            {countdown && (
              <div className="countdown-overlay">
                <span className="countdown-number">{countdown}</span>
              </div>
            )}

            {(!liveviewReady || liveviewError) && (
              <div className={liveviewError ? 'liveview-error' : 'liveview-loading'}>
                <span>📷</span>
                <p>Camera Sony a6400 đang sẵn sàng</p>
                <p>Session: {sessionId || 'đang tạo...'}</p>
                {liveviewError && <p>Mất kết nối liveview, đang thử kết nối lại...</p>}
              </div>
            )}
          </div>

          <div className="capture-overlay">
            <div className="capture-top-left">
              <button
                type="button"
                className="btn-back"
                onClick={goBackToFrameSelect}
                disabled={actionLoading.cancel}
              >
                {actionLoading.cancel ? 'Đang hủy...' : '← Đổi Khung'}
              </button>
            </div>

            <div className="capture-left-rail">
              <div className="timer-group timer-vertical">
                {COUNTDOWN_OPTIONS.map((time) => (
                  <button
                    key={time}
                    type="button"
                    className={`timer-btn ${selectedCountdownTime === time ? 'active' : ''}`}
                    onClick={() => setSelectedCountdownTime(time)}
                    disabled={actionLoading.capture}
                  >
                    {time}s
                  </button>
                ))}
              </div>
            </div>

            <div className="capture-center-rail">
              <button
                type="button"
                className={`capture-btn ${isCapturing || actionLoading.capture ? 'capturing' : ''}`}
                onClick={startCountdown}
                disabled={isCapturing || actionLoading.capture || actionLoading.process || isCaptureFull}
              >
                <span className="dot"></span>
              </button>
            </div>

            <div className="capture-top-right">
              <div className="photo-count photo-count-large photo-count-edge">
                <span className="count">{capturedPhotos.length}</span>
                <span className="label">/ {CAPTURE_TARGET}</span>
              </div>

              {capturedPhotos.length > 0 && (
                <div className="photos-strip photos-strip-vertical">
                  {capturedPhotos.map((photo, index) => (
                    <div key={photo.filename} className="strip-photo">
                      <img src={photo.previewUrl} alt={`Photo ${index + 1}`} />
                      <span className="photo-num">{index + 1}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div className="capture-right-rail">
              {canGoNext && (
                <button
                  type="button"
                  className="btn-next"
                  onClick={goToArrange}
                >
                  Tiếp →
                </button>
              )}
            </div>
          </div>
        </div>
      )}

      {currentStep === 'arrange' && selectedFrame && (
        <div className="fullscreen-step arrange-step">
          <div className="arrange-layout">
            <div className="frame-preview-container">
              <div
                className="frame-preview"
                style={{ aspectRatio: `${selectedFrame.outputWidth}/${selectedFrame.outputHeight}` }}
              >
                <img src={selectedFrame.template} alt="Frame" className="frame-template" />
                {selectedFrame.slots.map((slot, index) => {
                  const filename = arrangedPhotos[index]
                  const photo = filename ? getPhotoByFilename(filename) : null

                  return (
                    <div
                      key={index}
                      data-slot-index={index}
                      className={`slot ${filename ? 'filled' : 'empty'} ${activeSlot === index ? 'active' : ''} ${dragOverSlot === index ? 'drag-over' : ''}`}
                      style={{
                        left: `${(slot.x / selectedFrame.outputWidth) * 100}%`,
                        top: `${(slot.y / selectedFrame.outputHeight) * 100}%`,
                        width: `${(slot.width / selectedFrame.outputWidth) * 100}%`,
                        height: `${(slot.height / selectedFrame.outputHeight) * 100}%`,
                      }}
                    >
                      {photo ? (
                        <img src={photo.previewUrl} alt={`Slot ${index + 1}`} />
                      ) : (
                        <span className="slot-num">{index + 1}</span>
                      )}
                    </div>
                  )
                })}
              </div>
            </div>

            <div className="photo-selection">
              <h3>Chọn ảnh cho vị trí {activeSlot + 1}</h3>
              <p className="photo-hint">Click vào ảnh đã chọn để bỏ, click vào ảnh chưa chọn để thêm</p>
              <div className="photo-list">
                {capturedPhotos.map((photo) => {
                  const isSelected = arrangedPhotos.includes(photo.filename)
                  return (
                  <div
                    key={photo.filename}
                    className={`selectable-photo ${draggedPhoto === photo.filename ? 'dragging' : ''} ${isSelected ? 'used' : ''}`}
                    onClick={() => handlePhotoClick(photo.filename)}
                  >
                    <img src={photo.previewUrl} alt={photo.filename} />
                    {isSelected && <span className="used-indicator">ĐÃ CHỌN</span>}
                  </div>
                  )
                })}
              </div>
              <button type="button" className="btn-auto" onClick={autoArrangePhotos}>
                Tự động sắp xếp
              </button>
            </div>
          </div>

          <div className="arrange-nav">
            <button
              type="button"
              className="btn-primary"
              disabled={arrangedPhotos.some((filename) => filename === null) || actionLoading.process}
              onClick={generateFinalImage}
            >
              {actionLoading.process ? 'ĐANG XỬ LÝ...' : 'TẠO ẢNH ->'}
            </button>

          </div>
        </div>
      )}

      {currentStep === 'done' && (
        <div className="fullscreen-step done-step">
          <div className="background-elements" aria-hidden="true">
            {randomBackgroundElements.map((element) => (
              <img
                key={`done-${element.id}`}
                src={element.src}
                alt=""
                className="background-element"
                style={{
                  top: `${element.top}px`,
                  left: `${element.left}px`,
                  width: `${element.size}px`,
                  height: `${element.size}px`,
                  opacity: element.opacity,
                  '--base-rotate': `${element.rotate}deg`,
                  '--float-x': `${element.moveX}px`,
                  '--float-y': `${element.moveY}px`,
                  '--float-back-x': `${element.moveXBack}px`,
                  '--float-back-y': `${element.moveYBack}px`,
                  '--float-duration': `${element.duration}s`,
                  '--float-delay': `${element.delay}s`,
                }}
              />
            ))}
          </div>

          <div className="done-content">
            <h1>HOÀN THÀNH</h1>
            <p className="done-subtext">
              {finalResult
                ? 'Quét QR để tải ảnh về điện thoại'
                : showLocalPrintReady
                  ? 'Bản mềm đang lỗi mạng. Ảnh cục bộ vẫn sẵn sàng cho bước in tại quầy.'
                : 'Không thể tạo mã QR do kết nối bị gián đoạn.'}
            </p>

            <div className={`done-result-grid ${!hasQr ? 'single-column' : ''}`}>
              {(finalResult || showLocalPrintReady) && resolvedFinalImageUrl && (
                <>
                  <div className="final-image-wrapper">
                    <img src={resolvedFinalImageUrl} alt="Final" className="final-image" />
                  </div>

                  {hasQr && (
                    <div className="qr-wrapper">
                      <img
                        src={`data:image/png;base64,${finalResult?.qrCodeBase64 || ''}`}
                        alt="QR Download"
                        className="qr-image"
                      />
                    </div>
                  )}
                </>
              )}
            </div>

            {!finalResult && showFallbackForm && !fallbackSaved && (
              <div className="fallback-panel" role="alert">
                <h4>Kết nối bị gián đoạn, nhập SĐT để hỗ trợ gửi ảnh thủ công</h4>
                <p>Vui lòng nhập số di động Việt Nam để nhân viên liên hệ qua Zalo.</p>

                <div className="fallback-input-row">
                  <input
                    type="tel"
                    inputMode="numeric"
                    maxLength={10}
                    placeholder="Ví dụ: 0912345678"
                    value={fallbackPhone}
                    onChange={(event) => {
                      const digitsOnly = event.target.value.replace(/\D/g, '').slice(0, 10)
                      setFallbackPhone(digitsOnly)
                      setFallbackPhoneError('')
                    }}
                    disabled={fallbackSubmitting}
                  />
                  <button
                    type="button"
                    className="btn-secondary"
                    onClick={submitFallbackContact}
                    disabled={fallbackSubmitting}
                  >
                    {fallbackSubmitting ? 'ĐANG LƯU...' : 'LƯU SĐT'}
                  </button>
                </div>

                {fallbackPhoneError && <p className="fallback-error">{fallbackPhoneError}</p>}
                {fallbackSubmitMessage && <p className="fallback-success">{fallbackSubmitMessage}</p>}
              </div>
            )}

            {!finalResult && fallbackSaved && (
              <div className="fallback-thanks">
                <p>Xin lỗi vì sự bất tiện này.</p>
                <p>Đội ngũ sự kiện Vang Vàn Bàu Trúc.</p>
              </div>
            )}

            <div className="done-actions">
              {finalResult ? (
                <button type="button" className="btn-secondary" onClick={goBackToFrameSelect}>
                  CHỤP LẠI
                </button>
              ) : null}
              {!finalResult && fallbackSaved && !showLocalPrintReady && (
                <button type="button" className="btn-secondary" onClick={continueToLocalPrint}>
                  TIẾP TỤC
                </button>
              )}
              {!finalResult && showLocalPrintReady && (
                <button type="button" className="btn-secondary" onClick={goBackToFrameSelect}>
                  CHỤP LẠI
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

export default App
