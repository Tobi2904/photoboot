const DEFAULT_TIMEOUT_MS = 30000

export const API_BASE = (import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000').replace(/\/$/, '')

class ApiError extends Error {
	constructor(message, status, data = null) {
		super(message)
		this.name = 'ApiError'
		this.status = status
		this.data = data
	}
}

async function apiRequest(path, options = {}, timeoutMs = DEFAULT_TIMEOUT_MS) {
	const controller = new AbortController()
	const timeout = setTimeout(() => controller.abort(), timeoutMs)

	try {
		const response = await fetch(`${API_BASE}${path}`, {
			...options,
			headers: {
				'Content-Type': 'application/json',
				...(options.headers || {}),
			},
			signal: controller.signal,
		})

		const contentType = response.headers.get('content-type') || ''
		const isJson = contentType.includes('application/json')
		const data = isJson ? await response.json() : await response.text()

		if (!response.ok) {
			const detail = typeof data === 'object' && data !== null ? data.detail : null
			const fallback = response.statusText || 'Request failed'
			throw new ApiError(detail || fallback, response.status, data)
		}

		return data
	} catch (error) {
		if (error.name === 'AbortError') {
			throw new ApiError('Yeu cau qua thoi gian cho phep, vui long thu lai.', 408)
		}
		if (error instanceof ApiError) {
			throw error
		}
		throw new ApiError('Khong the ket noi den backend.', 0)
	} finally {
		clearTimeout(timeout)
	}
}

export function toAbsoluteUrl(relativeOrAbsolute) {
	if (!relativeOrAbsolute) return ''
	if (relativeOrAbsolute.startsWith('http://') || relativeOrAbsolute.startsWith('https://')) {
		return relativeOrAbsolute
	}
	return `${API_BASE}${relativeOrAbsolute}`
}

export async function listFrames() {
	return apiRequest('/api/frames', { method: 'GET' })
}

export async function startSession() {
	return apiRequest('/api/start_session', { method: 'POST' })
}

export async function cancelSession() {
	return apiRequest('/api/cancel_session', { method: 'POST' })
}

export async function capturePhoto() {
	return apiRequest('/api/capture', { method: 'POST' })
}

export async function processSession(payload) {
	return apiRequest('/api/process', {
		method: 'POST',
		body: JSON.stringify(payload),
	})
}

export { ApiError }
