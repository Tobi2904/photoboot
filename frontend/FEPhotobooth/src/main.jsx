import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.jsx'
import FrameEditor from './FrameEditor.jsx'

// Truy cập /editor để vào Frame Editor tool
// VD: http://localhost:5174/?editor
const isEditorMode = window.location.search.includes('editor')

createRoot(document.getElementById('root')).render(
  <StrictMode>
    {isEditorMode ? <FrameEditor /> : <App />}
  </StrictMode>,
)
