import { useState, useRef } from 'react'
import './FrameEditor.css'

// Import tất cả frames bạn muốn config
import frame2 from './assets/frame/Frame-2.png'
import frame3 from './assets/frame/Frame-3.png'

const AVAILABLE_FRAMES = [
  { id: 'frame_2', name: 'Frame 2 - Cham Di San', src: frame2 },
  { id: 'frame_3', name: 'Frame 3 - Am Vang Bau Truc', src: frame3 },
]

function FrameEditor() {
  const [selectedFrame, setSelectedFrame] = useState(AVAILABLE_FRAMES[0])
  const [slots, setSlots] = useState([])
  const [currentSlot, setCurrentSlot] = useState({ points: [] })
  const [imageSize, setImageSize] = useState({ width: 0, height: 0 })
  const imgRef = useRef(null)

  const handleImageLoad = (e) => {
    setImageSize({
      width: e.target.naturalWidth,
      height: e.target.naturalHeight
    })
  }

  const handleImageClick = (e) => {
    if (!imgRef.current) return

    const rect = imgRef.current.getBoundingClientRect()
    const scaleX = imageSize.width / rect.width
    const scaleY = imageSize.height / rect.height

    // Tọa độ thực trên ảnh gốc
    const x = Math.round((e.clientX - rect.left) * scaleX)
    const y = Math.round((e.clientY - rect.top) * scaleY)

    const newPoints = [...currentSlot.points, { x, y }]

    if (newPoints.length === 4) {
      // Tính bounding box từ 4 điểm
      const minX = Math.min(...newPoints.map(p => p.x))
      const maxX = Math.max(...newPoints.map(p => p.x))
      const minY = Math.min(...newPoints.map(p => p.y))
      const maxY = Math.max(...newPoints.map(p => p.y))

      const newSlot = {
        x: minX,
        y: minY,
        width: maxX - minX,
        height: maxY - minY,
        points: newPoints
      }

      setSlots([...slots, newSlot])
      setCurrentSlot({ points: [] })
    } else {
      setCurrentSlot({ points: newPoints })
    }
  }

  const removeSlot = (index) => {
    setSlots(slots.filter((_, i) => i !== index))
  }

  const resetAll = () => {
    setSlots([])
    setCurrentSlot({ points: [] })
  }

  const generateCode = () => {
    const previewSymbol = selectedFrame.id.replace('_', '')
    const code = `{
  id: '${selectedFrame.id}',
  name: '${selectedFrame.name}',
  preview: ${previewSymbol},
  template: ${previewSymbol},
  slots: [
${slots.map((slot, i) => `    { x: ${slot.x}, y: ${slot.y}, width: ${slot.width}, height: ${slot.height} }, // Slot ${i + 1}`).join('\n')}
  ],
  outputWidth: ${imageSize.width},
  outputHeight: ${imageSize.height},
}`
    return code
  }

  const copyCode = () => {
    navigator.clipboard.writeText(generateCode())
    alert('Đã copy code!')
  }

  return (
    <div className="frame-editor">
      <div className="editor-sidebar">
        <h1>🖼️ Frame Editor</h1>
        
        <div className="frame-select">
          <label>Chọn Frame:</label>
          <select 
            value={selectedFrame.id} 
            onChange={(e) => {
              const frame = AVAILABLE_FRAMES.find(f => f.id === e.target.value)
              setSelectedFrame(frame)
              resetAll()
            }}
          >
            {AVAILABLE_FRAMES.map(f => (
              <option key={f.id} value={f.id}>{f.name}</option>
            ))}
          </select>
        </div>

        <div className="instructions">
          <h3>Hướng dẫn:</h3>
          <ol>
            <li>Click 4 góc của mỗi ô ảnh (theo thứ tự bất kỳ)</li>
            <li>Tool sẽ tự tính bounding box</li>
            <li>Lặp lại cho tất cả các ô</li>
            <li>Copy code và paste vào App.jsx</li>
          </ol>
        </div>

        <div className="current-status">
          <p>Điểm đang chọn: <strong>{currentSlot.points.length}/4</strong></p>
          <p>Số slot đã tạo: <strong>{slots.length}</strong></p>
        </div>

        <div className="slot-list">
          <h3>Slots đã tạo:</h3>
          {slots.map((slot, index) => (
            <div key={index} className="slot-item">
              <span>Slot {index + 1}: ({slot.x}, {slot.y}) - {slot.width}x{slot.height}</span>
              <button onClick={() => removeSlot(index)}>×</button>
            </div>
          ))}
        </div>

        <div className="actions">
          <button className="btn-reset" onClick={resetAll}>🔄 Reset</button>
          <button className="btn-copy" onClick={copyCode} disabled={slots.length === 0}>
            📋 Copy Code
          </button>
        </div>

        {slots.length > 0 && (
          <div className="code-preview">
            <h3>Code:</h3>
            <pre>{generateCode()}</pre>
          </div>
        )}

        <div className="image-info">
          <p>Kích thước ảnh: {imageSize.width} x {imageSize.height}</p>
        </div>
      </div>

      <div className="editor-canvas">
        <div className="image-container">
          <img
            ref={imgRef}
            src={selectedFrame.src}
            alt={selectedFrame.name}
            onLoad={handleImageLoad}
            onClick={handleImageClick}
          />
          
          {/* Hiển thị các điểm đang click */}
          {currentSlot.points.map((point, index) => (
            <div
              key={`current-${index}`}
              className="point current"
              style={{
                left: `${(point.x / imageSize.width) * 100}%`,
                top: `${(point.y / imageSize.height) * 100}%`
              }}
            >
              {index + 1}
            </div>
          ))}

          {/* Hiển thị các slot đã hoàn thành */}
          {slots.map((slot, index) => (
            <div
              key={`slot-${index}`}
              className="slot-overlay"
              style={{
                left: `${(slot.x / imageSize.width) * 100}%`,
                top: `${(slot.y / imageSize.height) * 100}%`,
                width: `${(slot.width / imageSize.width) * 100}%`,
                height: `${(slot.height / imageSize.height) * 100}%`
              }}
            >
              <span>{index + 1}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

export default FrameEditor
