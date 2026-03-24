import { useState, useRef, useEffect, useCallback } from 'react'
import './App.css'
import {
  API_BASE,
  ApiError,
  cancelSession,
  capturePhoto,
  listFrames,
  processSession,
  startSession,
  toAbsoluteUrl,
} from './api/client'

const COUNTDOWN_OPTIONS = [0, 3, 5, 10]
const CAPTURE_TARGET = 5

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
    return 'Da xay ra loi khong xac dinh. Vui long thu lai.'
  }

  if (error.status === 503) {
    return 'Camera Sony dang offline hoac ban. Vui long kiem tra ket noi USB va thu lai.'
  }
  if (error.status === 502) {
    return 'Upload anh len S3 that bai. Vui long thu lai sau it giay.'
  }
  if (error.status === 400) {
    return error.message || 'Yeu cau khong hop le. Vui long kiem tra thao tac.'
  }
  if (error.status === 404) {
    return error.message || 'Khong tim thay tai nguyen.'
  }
  if (error.status === 408) {
    return 'Yeu cau bi timeout. Vui long thu lai.'
  }
  if (error.status === 0) {
    return 'Khong ket noi duoc backend. Kiem tra server tai ' + API_BASE
  }
  return error.message || 'Da xay ra loi he thong.'
}

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

  const countdownIntervalRef = useRef(null)
  const sessionIdRef = useRef(sessionId)
  const touchRef = useRef({ photoFilename: null })

  useEffect(() => {
    sessionIdRef.current = sessionId
  }, [sessionId])

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
      setErrorMessage(`Da chup du ${maxPhotos} anh cho khung nay.`)
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
    await cancelActiveSession(false)
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
      setErrorMessage('O hinh da full. Vui long bo chon mot hinh trong khung de them anh moi.')
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
      setSessionId(null)
      setCurrentStep('done')
    } catch (error) {
      setErrorMessage(getErrorMessage(error))
    } finally {
      setActionLoading((prev) => ({ ...prev, process: false }))
    }
  }

  const slotCount = selectedFrame?.slots.length || 0
  const isCaptureFull = capturedPhotos.length >= CAPTURE_TARGET
  const canGoNext = capturedPhotos.length >= CAPTURE_TARGET

  return (
    <div className="app">
      {errorMessage && <div className="status-banner error">{errorMessage}</div>}

      {currentStep === 'select-frame' && (
        <div className="fullscreen-step frame-selection">
          <div className="center-content">
            <h1 className="main-title">PHOTOBOOTH</h1>
            <p className="subtitle">Chon khung anh de bat dau</p>

            {isLoadingFrames ? (
              <div className="liveview-loading">
                <div className="spinner"></div>
                <p>Dang tai danh sach frame...</p>
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
              {actionLoading.startSession ? 'DANG TAO SESSION...' : 'BAT DAU CHUP ->'}
            </button>
          </div>
        </div>
      )}

      {currentStep === 'capture' && (
        <div className="fullscreen-step capture-step">
          <div className="liveview-fullscreen">
            {countdown && (
              <div className="countdown-overlay">
                <span className="countdown-number">{countdown}</span>
              </div>
            )}

            <div className="liveview-loading">
              <span>📷</span>
              <p>Camera Sony a6400 dang san sang</p>
              <p>Session: {sessionId || 'dang tao...'}</p>
            </div>
          </div>

          <div className="capture-overlay">
            <div className="top-bar">
              <button
                type="button"
                className="btn-back"
                onClick={goBackToFrameSelect}
                disabled={actionLoading.cancel}
              >
                {actionLoading.cancel ? 'Dang huy...' : '← Doi Khung'}
              </button>
            </div>

            <div className="bottom-bar">
              <div className="timer-group">
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

              <div className="capture-group">
                <div className="photo-count">
                  <span className="count">{capturedPhotos.length}</span>
                  <span className="label">/ {CAPTURE_TARGET}</span>
                </div>
                <button
                  type="button"
                  className={`capture-btn ${isCapturing || actionLoading.capture ? 'capturing' : ''}`}
                  onClick={startCountdown}
                  disabled={isCapturing || actionLoading.capture || actionLoading.process || isCaptureFull}
                >
                  <span className="dot"></span>
                </button>
              </div>

              <div className="next-group">
                {canGoNext && (
                  <button
                    type="button"
                    className="btn-next"
                    onClick={goToArrange}
                  >
                    Tiep →
                  </button>
                )}

                {capturedPhotos.length > 0 && (
                  <div className="photos-strip">
                    {capturedPhotos.map((photo, index) => (
                      <div key={photo.filename} className="strip-photo">
                        <img src={photo.previewUrl} alt={`Photo ${index + 1}`} />
                        <span className="photo-num">{index + 1}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
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
              <h3>Chon anh cho vi tri {activeSlot + 1}</h3>
              <p className="photo-hint">Click vao anh da chon de bo, click vao anh chua chon de them</p>
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
                    {isSelected && <span className="used-indicator">DA CHON</span>}
                  </div>
                  )
                })}
              </div>
              <button type="button" className="btn-auto" onClick={autoArrangePhotos}>
                Tu dong sap xep
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
              {actionLoading.process ? 'DANG XU LY...' : 'TAO ANH ->'}
            </button>
          </div>
        </div>
      )}

      {currentStep === 'done' && finalResult && (
        <div className="fullscreen-step done-step">
          <h1>HOAN THANH</h1>
          <p className="done-subtext">Quet QR de tai anh ve dien thoai</p>

          <div className="done-result-grid">
            <div className="final-image-wrapper">
              <img src={finalResult.finalImageUrl} alt="Final" className="final-image" />
            </div>

            <div className="qr-wrapper">
              <img
                src={`data:image/png;base64,${finalResult.qrCodeBase64}`}
                alt="QR Download"
                className="qr-image"
              />
            </div>
          </div>

          <div className="done-actions">
            <button type="button" className="btn-secondary" onClick={goBackToFrameSelect}>
              CHUP LAI
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

export default App
