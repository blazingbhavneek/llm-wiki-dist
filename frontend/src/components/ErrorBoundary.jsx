import { Component } from 'react'
import { useT } from '../i18n.jsx'

const STR = {
  ja: {
    title: 'このビューでエラーが発生しました',
    hint: 'タブを切り替えるか、別のノートを開いてください — アプリの他の部分は引き続き動作しています。',
  },
  en: {
    title: 'Something went wrong in this view',
    hint: 'Switch tabs or open a different note — the rest of the app is still working.',
  },
}

function ErrorFallback({ error }) {
  const t = useT(STR)
  return (
    <div className="grid h-full place-items-center p-6">
      <div className="max-w-[420px] text-center">
        <p className="font-bold text-red">{t.title}</p>
        <p className="mt-2 text-[13px] text-muted">{String(error?.message || error)}</p>
        <p className="mt-3 text-[13px] text-muted">{t.hint}</p>
      </div>
    </div>
  )
}

// サブツリー内のレンダーエラーを捕捉し、壊れたビュー
// （例: 不正な Markdown ドキュメント）がアプリ全体を真っ白にしないようにします。
// `resetKey` が変わったとき（タブやノートの切り替え時）に再マウントします。
export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidUpdate(prev) {
    if (prev.resetKey !== this.props.resetKey && this.state.error) {
      this.setState({ error: null })
    }
  }

  render() {
    if (this.state.error) {
      return <ErrorFallback error={this.state.error} />
    }
    return this.props.children
  }
}
