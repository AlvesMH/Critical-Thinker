import { useMemo, useState } from 'react'
import ReactMarkdown from 'react-markdown'

const AGENTS = [
  { key: 'science', label: 'Science', description: 'Feasibility & uncertainty' },
  { key: 'economics', label: 'Economics', description: 'Incentives & externalities' },
  { key: 'sociology', label: 'Sociology/Humanities', description: 'Social context & inequality' },
  { key: 'ethics', label: 'Ethics', description: 'Rights & justice' }
]

const API_PREFIX = '/api'

const EXAMPLE_TEXT = `Policies that accelerate renewable energy adoption are essential to economic stability.\n\nGovernments should mandate a rapid transition to clean power within 10 years. This will reduce long-term energy costs, create green jobs, and protect public health. Fossil fuels impose hidden costs through pollution and climate damage. While the transition is expensive upfront, the benefits outweigh the costs for future generations.`

export default function App() {
  const [mode, setMode] = useState('text')
  const [textValue, setTextValue] = useState('')
  const [fileValue, setFileValue] = useState(null)
  const [analysis, setAnalysis] = useState(null)
  const [selectedAgent, setSelectedAgent] = useState('science')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [downloadLoading, setDownloadLoading] = useState(false)
  const [answerLength, setAnswerLength] = useState('long')

  const isReadyToAnalyze = useMemo(() => {
    if (mode === 'text') {
      return textValue.trim().length > 0
    }
    return Boolean(fileValue)
  }, [mode, textValue, fileValue])

  const handleAnalyze = async () => {
    setError('')
    if (!isReadyToAnalyze) {
      setError('Please enter text or upload a PDF to continue.')
      return
    }

    setLoading(true)
    setAnalysis(null)
    try {
      let response
      if (mode === 'pdf' && fileValue) {
        const formData = new FormData()
        formData.append('file', fileValue)
        formData.append('answer_length', answerLength)
        response = await fetch(`${API_PREFIX}/analyze`, {
          method: 'POST',
          body: formData
        })
      } else {
        response = await fetch(`${API_PREFIX}/analyze`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: textValue, answer_length: answerLength })
        })
      }

      if (!response.ok) {
        const detail = await response.json().catch(() => ({}))
        throw new Error(detail.detail || 'Analysis failed. Please try again.')
      }

      const data = await response.json()
      setAnalysis(data.analysis)
      if (data?.meta?.answer_length) setAnswerLength(data.meta.answer_length)
      setSelectedAgent('science')
      document.getElementById('results').scrollIntoView({ behavior: 'smooth' })
    } catch (err) {
      setError(err.message || 'Something went wrong. Please try again.')
    } finally {
      setLoading(false)
    }
  }

  const handleDownload = async () => {
    if (!analysis) return
    setDownloadLoading(true)
    try {
      const response = await fetch(`${API_PREFIX}/generate-pdf`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ analysis, answer_length: answerLength })
      })
      if (!response.ok) {
        throw new Error('Failed to generate PDF report.')
      }
      const blob = await response.blob()
      const url = window.URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = 'CriticalThinkingReport.pdf'
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      window.URL.revokeObjectURL(url)
    } catch (err) {
      setError(err.message || 'Unable to download the report.')
    } finally {
      setDownloadLoading(false)
    }
  }

  const activeAnalysis = analysis?.[selectedAgent]

  return (
    <div className="app">
      <header className="hero">
        <div className="hero__content">
          <p className="eyebrow">Critical Thinking Analysis</p>
          <h1>Critical Thinker</h1>
          <p className="subtitle">
            Multi-perspective critique of arguments using Paul &amp; Elder’s framework and the
            Gemma-SEA-LION 27B model.
          </p>
        </div>
        <div className="hero__note">
          <h2>What you get</h2>
          <ul>
            <li>Four expert lenses: Science, Economics, Sociology/Humanities, Ethics.</li>
            <li>Structured feedback on Elements of Reasoning + Intellectual Standards.</li>
            <li>Downloadable PDF report for sharing or review.</li>
          </ul>
        </div>
      </header>

      <main>
        <section className="input-panel" aria-label="Input your argument">
          <div className="input-panel__header">
            <div>
              <h2>Submit your argument</h2>
              <p>Paste text or upload a PDF. We only analyze the text content.</p>
            </div>
            <div className="mode-toggle" role="tablist" aria-label="Input mode">
              <button
                type="button"
                className={mode === 'text' ? 'active' : ''}
                onClick={() => setMode('text')}
                role="tab"
                aria-selected={mode === 'text'}
              >
                Text
              </button>
              <button
                type="button"
                className={mode === 'pdf' ? 'active' : ''}
                onClick={() => setMode('pdf')}
                role="tab"
                aria-selected={mode === 'pdf'}
              >
                PDF
              </button>
            </div>
          </div>
          <div className="mode-toggle" role="group" aria-label="Answer length">
            <button
              type="button"
              className={answerLength === 'long' ? 'active' : ''}
              onClick={() => setAnswerLength('long')}
              aria-pressed={answerLength === 'long'}
            >
              Long Answer
            </button>
            <button
              type="button"
              className={answerLength === 'short' ? 'active' : ''}
              onClick={() => setAnswerLength('short')}
              aria-pressed={answerLength === 'short'}
            >
              Short Answer
            </button>
          </div>


          {mode === 'text' ? (
            <div className="input-group">
              <label htmlFor="argument">Argument text</label>
              <textarea
                id="argument"
                rows="8"
                value={textValue}
                onChange={(event) => setTextValue(event.target.value)}
                placeholder="Paste or type your argument here..."
              />
              <div className="input-footer">
                <span>{textValue.length.toLocaleString()} / 100,000 characters</span>
                <button
                  type="button"
                  className="ghost"
                  onClick={() => setTextValue(EXAMPLE_TEXT)}
                >
                  Use example
                </button>
              </div>
            </div>
          ) : (
            <div className="input-group">
              <label htmlFor="pdf-upload">Upload a PDF (max 10MB)</label>
              <div className="file-upload">
                <input
                  id="pdf-upload"
                  type="file"
                  accept="application/pdf"
                  onChange={(event) => setFileValue(event.target.files?.[0] || null)}
                />
                <div>
                  <strong>{fileValue ? fileValue.name : 'Drag a PDF or click to browse.'}</strong>
                  <p>Images and scanned pages will not be analyzed.</p>
                </div>
              </div>
            </div>
          )}

          {error && <div className="alert" role="alert">{error}</div>}

          <div className="actions">
            <button
              type="button"
              className="primary"
              onClick={handleAnalyze}
              disabled={loading}
            >
              {loading ? 'Analyzing…' : 'Analyze'}
            </button>
            <p className="disclaimer">
              Your input is sent to an AI service for analysis. No data is stored on our servers.
            </p>
          </div>
        </section>

        <section id="results" className="results" aria-live="polite">
          <div className="results__header">
            <div>
              <h2>Analysis results</h2>
              <p>Select a perspective to explore the critique.</p>
            </div>
            <button
              type="button"
              className="secondary"
              onClick={handleDownload}
              disabled={!analysis || downloadLoading}
            >
              {downloadLoading ? 'Preparing PDF…' : 'Download report'}
            </button>
          </div>

          <div className="agent-tabs" role="tablist" aria-label="Agent selection">
            {AGENTS.map((agent) => (
              <button
                key={agent.key}
                type="button"
                role="tab"
                aria-selected={selectedAgent === agent.key}
                className={selectedAgent === agent.key ? 'active' : ''}
                onClick={() => setSelectedAgent(agent.key)}
                disabled={!analysis}
              >
                <span>{agent.label}</span>
                <small>{agent.description}</small>
              </button>
            ))}
          </div>

          <div className="analysis-card">
            {!analysis && (
              <div className="empty">
                <h3>Ready when you are</h3>
                <p>Submit text or a PDF to receive a structured critique from four expert agents.</p>
              </div>
            )}

            {analysis && activeAnalysis?.status === 'error' && (
              <div className="error-state">
                <h3>Analysis unavailable</h3>
                <p>{activeAnalysis.message}</p>
              </div>
            )}

            {analysis && activeAnalysis?.status === 'ok' && (
              <ReactMarkdown>{activeAnalysis.content}</ReactMarkdown>
            )}
          </div>
        </section>
      </main>

      <footer className="footer">
        <p>
          Built for multi-perspective argument review. Powered by FastAPI + React on Render.
        </p>
      </footer>
    </div>
  )
}
