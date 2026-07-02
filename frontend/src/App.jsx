import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  BookMarked,
  BookOpen,
  Bookmark,
  ChevronLeft,
  ChevronRight,
  Clock3,
  FileText,
  FolderTree,
  Languages,
  LayoutDashboard,
  Loader2,
  MessageCircle,
  Minimize2,
  Network,
  PanelLeftClose,
  PanelLeftOpen,
  PlusCircle,
  Search,
  Settings,
  Sparkles,
  Tags,
  Upload,
  UserCircle,
} from 'lucide-react'

import ChatPanel from './components/ChatPanel'
import GraphCanvas from './components/GraphCanvas'
import MarkdownView from './components/MarkdownView'
import ErrorBoundary from './components/ErrorBoundary'
import DocSidebar from './components/DocSidebar'
import SettingsView from './components/SettingsView'
import UploadView from './components/UploadView'
import { api } from './api'
import { useT, LangToggle } from './i18n.jsx'
import { layoutGraph, docFromNode } from './data/layout'
import { reconstructDocument } from './data/docs'

import { buildLibrary } from './data/docs'




const STR = {
  ja: {
    brand: 'LLM Wiki',
    brandSubtitle: 'ナレッジワークスペース',

    pinned: {
      home: 'スタート',
      graph: 'グラフ',
      upload: 'アップロード',
      settings: '設定',
      chat: 'チャット',
      glossary: '用語集',
      topics: 'トピック',
      documents: 'ドキュメント',
    },

    shell: {
      chat: 'チャット',
      graph: 'グラフ',
      upload: 'アップロード',
      glossary: '用語集',
      topics: 'トピック',
      settings: '設定',
      newChat: '新しいチャット',
      recentQuestions: '最近の質問',
      viewAll: 'すべて表示',
      noRecentQuestions: '最近の質問はありません。',
      collapseSidebar: 'サイドバーを折りたたむ',
      expandSidebar: 'サイドバーを展開',
      history: '履歴',
      bookmark: 'ブックマーク',
      profile: 'プロフィール',
      collapseDocuments: 'ドキュメントを閉じる',
      showDocuments: 'ドキュメントを表示',
      closeRightSidebar: '右サイドバーを閉じる',
      switchToDocs: 'ドキュメント',
      switchToTopics: 'トピック',
      terms: '利用規約',
      privacy: 'プライバシーポリシー',
      footerCopyright: (year) => `© ${year} LLM Wiki Project`,
    },

    topbar: {
      placeholder: 'ノートまたはトピックを検索…',
      keywordSearch: 'キーワード検索',
      searchDocs: '検索',
      searching: '検索中…',
      emptyQuery: '検索語を入力してください。',
    },

    pages: {
      uploadTitle: 'アップロード',
      uploadText: 'Markdown またはソースファイルをナレッジグラフに取り込みます。',
      settingsTitle: '設定',
      settingsText: 'エージェントの動作とリクエスト単位の上書きを設定します。',
      glossaryTitle: '用語集',
      glossaryText: '用語集は後で実装します。',
      searchTitle: '検索結果',
      searchText: (q, n) => `「${q}」に一致するノートが ${n} 件見つかりました。`,
      searchEmptyTitle: '検索結果がありません',
      searchEmptyText: (q) => `「${q}」に一致するノートは見つかりませんでした。`,
      searchIdleTitle: '検索してください',
      searchIdleText: '上部の検索バーからノートまたはトピックを検索できます。',
    },

    rightRail: {
      documentsTitle: 'ドキュメント',
      documentsSubtitle: '元ドキュメントのナビゲーション',
      topicsTitle: 'トピック',
      topicsSubtitle: 'トピック別に整理',
    },

    markdownFrame: {
      fallbackTitle: 'ドキュメント',
      hint: 'ドキュメント領域をダブルクリックすると編集できます。折りたたむとこの表示を閉じます。',
      working: '処理中…',
      collapse: '折りたたむ',
      collapseTitle: 'ドキュメントを折りたたむ',
    },

    searchResults: {
      open: '開く',
      sourceNote: 'ソースノート',
      agentNote: 'エージェントノート',
      unknownSource: 'ソース不明',
      noSummary: '概要はありません。',
      resultCount: (n) => `${n} 件`,
    },

    startingWrite: '書き込みジョブを開始中...',
    queued: '書き込みジョブをキューに入れました。',
    queuedPos: (p) => `書き込みジョブをキューに入れました。順位 ${p}。`,
    writing: 'グラフに書き込み中：埋め込み、リンク、クラスタリング...',
    writeFinished: '書き込みが完了しました。',
    writeFailed: '書き込みジョブが失敗しました。',
    writeCancelled: '書き込みジョブがキャンセルされました。',
    statusOf: (s) => `書き込みジョブの状態: ${s}`,

    couldNotOpen: (m) => `ノートを開けませんでした: ${m}`,
    startingUpdate: 'グラフの更新を開始中...',
    nodeUpdated: 'ノードをグラフで更新しました。',
    savedNodeUpdated: '保存しました。ノードをグラフで更新しました。',
    updateFailed: (m) => `更新に失敗しました: ${m}`,
    startingDelete: '削除ジョブを開始中...',
    nodeDeleted: 'ノードをグラフから削除しました。',
    noteDeleted: 'ノートを削除しました。',
    deleteFailed: (m) => `削除に失敗しました: ${m}`,

    startingWriteGraph: 'グラフへの書き込みを開始中...',
    mdAdded: 'Markdown をグラフに追加しました。',
    mdAddedOpened: 'Markdown をグラフに追加し、ワークスペースで開きました。',
    addFailed: (m) => `追加に失敗しました: ${m}`,
    addedToGraph: 'グラフに追加しました。',
    savedOpened: 'グラフに保存し、ワークスペースで開きました。',
    savedToGraph: 'グラフに保存しました。',
    savedWikiNote: 'wiki ノートとして保存し、ソースにリンクしました。',
    saveFailed: (m) => `保存に失敗しました: ${m}`,

    noMatch: '一致するノートはありません。',
    searchFailed: (m) => `検索に失敗しました: ${m}`,

    agentNote: 'エージェントノート',
    sourceNote: 'ソースノート',
    untitled: '無題',
    answer: '回答',

    fullDocMeta: (n) => `${n} ページから再構成した完全なドキュメント。`,
    unsavedDraft: '未保存の下書き。',
    answerMetaSteps: (n) => `${n} ステップで生成。確認・編集してから wiki に追加してください。`,
    answerMeta: '確認・編集してから wiki に追加してください。',

    explorer: (n) => `エクスプローラー ${n}`,
    searching: (who, q) => (who ? `${who} · “${q}” を検索中` : `“${q}” を検索中`),
    pagesFound: (c) => `${c} 件のページが見つかりました`,
    spawned: (n) => `${n} 人のエクスプローラーを起動`,
    exploring: (who, n) => `${who} · ${n} を探索中`,
    reading: (who, n) => `${who} · ${n} を読み中`,
    following: (who, n, ne) => `${who} · ${n} からリンクをたどり中 (${ne})`,
    subDone: (who, c) => `${who} · 完了 (${c} ソース)`,
    compiling: '回答をコンパイル中…',
    diagramBuilding: '図を作成中…',
    diagramReady: '図の準備完了',
    diagramFailed: '図を描画できませんでした',

    working: '作業中…',
    answerReady: (steps) => `${steps} ステップで回答ができました。`,
    openedReview: 'ワークスペースで開きました →。確認してから wiki に追加してください。',
    foundNoBody: 'ソースは見つかりましたが、回答本文がありません。',
    foundNoBodyText: (c, steps) =>
      `エージェントは ${steps} ステップで ${c} 件のソースを集めましたが、最終的な回答を生成しませんでした。`,
    requestFailed: 'リクエストが失敗しました',

    collapseDocs: 'ドキュメントを隠す',
    showDocs: 'ドキュメントを表示',
    loadingGraph: 'グラフを読み込み中…',
    cannotReach: 'バックエンドに接続できません',

    closeTab: 'タブを閉じる',
    nothingOpen: '何も開いていません',
    homeHint: (hasDocs) =>
      `左のパネルからドキュメントを開くか${hasDocs ? '' : '（取り込み後）'}、チャットでエージェントに作成を依頼してください。`,

    assimTitle:
      '新しいノードは今すぐ検索できます。ランキングとクラスタリングはバックグラウンドで進行中です。',
    assimBadge: 'グラフを整理中…',
  },

  en: {
    brand: 'LLM Wiki',
    brandSubtitle: 'Knowledge workspace',

    pinned: {
      home: 'Start',
      graph: 'Graph',
      upload: 'Upload',
      settings: 'Settings',
      chat: 'Chat',
      glossary: 'Glossary',
      topics: 'Topics',
      documents: 'Documents',
    },

    shell: {
      chat: 'Chat',
      graph: 'Graph',
      upload: 'Upload',
      glossary: 'Glossary',
      topics: 'Topics',
      settings: 'Settings',
      newChat: 'New chat',
      recentQuestions: 'Recent questions',
      viewAll: 'View all',
      noRecentQuestions: 'No recent questions yet.',
      collapseSidebar: 'Collapse sidebar',
      expandSidebar: 'Expand sidebar',
      history: 'History',
      bookmark: 'Bookmark',
      profile: 'Profile',
      collapseDocuments: 'Hide documents',
      showDocuments: 'Show documents',
      closeRightSidebar: 'Close right sidebar',
      switchToDocs: 'Docs',
      switchToTopics: 'Topics',
      terms: 'Terms',
      privacy: 'Privacy Policy',
      footerCopyright: (year) => `© ${year} LLM Wiki Project`,
    },

    topbar: {
      placeholder: 'Search notes or topics…',
      keywordSearch: 'Keyword search',
      searchDocs: 'Search docs',
      searching: 'Searching…',
      emptyQuery: 'Enter a search term.',
    },

    pages: {
      uploadTitle: 'Upload',
      uploadText: 'Import Markdown or source files into the knowledge graph.',
      settingsTitle: 'Settings',
      settingsText: 'Configure agent behavior and request-level overrides.',
      glossaryTitle: 'Glossary',
      glossaryText: 'Glossary will be implemented later.',
      searchTitle: 'Search results',
      searchText: (q, n) => `${n} matching notes found for “${q}”.`,
      searchEmptyTitle: 'No search results',
      searchEmptyText: (q) => `No matching notes were found for “${q}”.`,
      searchIdleTitle: 'Search knowledge',
      searchIdleText: 'Use the top search bar to find notes or topics.',
    },

    rightRail: {
      documentsTitle: 'Documents',
      documentsSubtitle: 'Original document navigation',
      topicsTitle: 'Topics',
      topicsSubtitle: 'Arranged by topic',
    },

    markdownFrame: {
      fallbackTitle: 'Document',
      hint: 'Double-click the document area to edit. Collapse closes this view.',
      working: 'Working…',
      collapse: 'Collapse',
      collapseTitle: 'Collapse document',
    },

    searchResults: {
      open: 'Open',
      sourceNote: 'Source note',
      agentNote: 'Agent note',
      unknownSource: 'Unknown source',
      noSummary: 'No summary available.',
      resultCount: (n) => `${n} results`,
    },

    startingWrite: 'Starting write job...',
    queued: 'Queued write job.',
    queuedPos: (p) => `Queued write job. Position ${p}.`,
    writing: 'Writing to the graph: embedding, linking, and clustering...',
    writeFinished: 'Write finished.',
    writeFailed: 'Write job failed.',
    writeCancelled: 'Write job was cancelled.',
    statusOf: (s) => `Write job status: ${s}`,

    couldNotOpen: (m) => `Could not open note: ${m}`,
    startingUpdate: 'Starting graph update...',
    nodeUpdated: 'Node updated in the graph.',
    savedNodeUpdated: 'Saved. Node updated in the graph.',
    updateFailed: (m) => `Update failed: ${m}`,
    startingDelete: 'Starting delete job...',
    nodeDeleted: 'Node deleted from the graph.',
    noteDeleted: 'Note deleted.',
    deleteFailed: (m) => `Delete failed: ${m}`,

    startingWriteGraph: 'Starting graph write...',
    mdAdded: 'Markdown added to the graph.',
    mdAddedOpened: 'Markdown added to the graph and opened in the workspace.',
    addFailed: (m) => `Add failed: ${m}`,
    addedToGraph: 'Added to the graph.',
    savedOpened: 'Saved to the graph and opened in the workspace.',
    savedToGraph: 'Saved to the graph.',
    savedWikiNote: 'Saved as a wiki note, linked to its sources.',
    saveFailed: (m) => `Save failed: ${m}`,

    noMatch: 'No matching notes.',
    searchFailed: (m) => `Search failed: ${m}`,

    agentNote: 'Agent note',
    sourceNote: 'Source note',
    untitled: 'Untitled',
    answer: 'Answer',

    fullDocMeta: (n) => `Full document reconstructed from ${n} pages.`,
    unsavedDraft: 'Unsaved draft.',
    answerMetaSteps: (n) => `Generated in ${n} steps. Review, edit, then add it to the wiki.`,
    answerMeta: 'Review, edit, then add it to the wiki.',

    explorer: (n) => `Explorer ${n}`,
    searching: (who, q) => (who ? `${who} · searching “${q}”` : `searching “${q}”`),
    pagesFound: (c) => `${c} pages found`,
    spawned: (n) => `spawned ${n} explorers`,
    exploring: (who, n) => `${who} · exploring ${n}`,
    reading: (who, n) => `${who} · reading ${n}`,
    following: (who, n, ne) => `${who} · following links from ${n} (${ne})`,
    subDone: (who, c) => `${who} · done (${c} sources)`,
    compiling: 'compiling answer…',
    diagramBuilding: 'building diagram…',
    diagramReady: 'diagram ready',
    diagramFailed: 'could not render diagram',

    working: 'Working…',
    answerReady: (steps) => `Answer ready in ${steps} steps.`,
    openedReview: 'Opened in the workspace →. Review it, then add it to the wiki.',
    foundNoBody: 'Found sources, but no answer body.',
    foundNoBodyText: (c, steps) =>
      `The agent gathered ${c} sources in ${steps} steps but produced no final answer.`,
    requestFailed: 'Request failed',

    collapseDocs: 'Collapse documents',
    showDocs: 'Show documents',
    loadingGraph: 'Loading graph…',
    cannotReach: 'Cannot reach the backend',

    closeTab: 'Close tab',
    nothingOpen: 'Nothing open',
    homeHint: (hasDocs) =>
      `Open a document from the left panel${hasDocs ? '' : ' once you ingest some'}, or ask the agent in the chat to write one.`,

    assimTitle:
      'New nodes are searchable now; ranking and clustering are catching up in the background.',
    assimBadge: 'Assimilating graph…',
  },
}

// -----------------------------------------------------------------------------
// Shell components
// -----------------------------------------------------------------------------

function LeftSidebar({
  collapsed,
  activeView,
  rightMode,
  recentQuestions,
  onToggle,
  onNavigate,
  onNewChat,
}) {
  const t = useT(STR)

  const items = [
    {
      id: 'chat',
      label: t.shell.chat,
      icon: MessageCircle,
      view: 'chat',
    },
    {
      id: 'graph',
      label: t.shell.graph,
      icon: Network,
      view: 'graph',
    },
    {
      id: 'upload',
      label: t.shell.upload,
      icon: Upload,
      view: 'upload',
    },
    {
      id: 'glossary',
      label: t.shell.glossary,
      icon: BookMarked,
      view: 'glossary',
    },
    {
      id: 'topics',
      label: t.shell.topics,
      icon: Tags,
      view: 'topics',
    },
  ]

  return (
    <aside
      className={`flex h-full shrink-0 flex-col border-r border-slate-200 bg-white transition-[width] duration-300 ${
        collapsed ? 'w-[76px]' : 'w-[240px]'
      }`}
    >
      <div className="border-b border-slate-100 px-3 py-4">
        <div
          className={`flex items-center gap-3 ${
            collapsed ? 'justify-center' : ''
          }`}
        >
          <div className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-blue-600 text-white shadow-sm">
            <BookOpen size={22} />
          </div>

          {!collapsed && (
            <div className="min-w-0 flex-1">
              <div className="truncate text-[17px] font-extrabold tracking-tight text-slate-950">
                {t.brand}
              </div>
              <div className="truncate text-[11px] font-medium text-slate-400">
                {t.brandSubtitle}
              </div>
            </div>
          )}
        </div>

        <button
          onClick={onToggle}
          className={`mt-3 grid h-8 place-items-center rounded-lg text-slate-500 hover:bg-slate-100 hover:text-slate-900 ${
            collapsed ? 'mx-auto w-10' : 'w-full'
          }`}
          title={collapsed ? t.shell.expandSidebar : t.shell.collapseSidebar}
          aria-label={collapsed ? t.shell.expandSidebar : t.shell.collapseSidebar}
        >
          {collapsed ? <PanelLeftOpen size={18} /> : <PanelLeftClose size={18} />}
        </button>
      </div>

      <nav className="flex-1 overflow-y-auto px-3 py-4">
        <div className="space-y-1">
          {items.map((item) => {
            const Icon = item.icon
            const active =
              item.view === 'topics'
                ? rightMode === 'topics'
                : activeView === item.view

            return (
              <button
                key={item.id}
                onClick={() => onNavigate(item.view)}
                className={`group flex h-10 w-full items-center gap-3 rounded-xl px-3 text-left text-[14px] font-semibold transition ${
                  active
                    ? 'bg-blue-50 text-blue-700'
                    : 'text-slate-600 hover:bg-slate-100 hover:text-slate-950'
                } ${collapsed ? 'justify-center' : ''}`}
                title={collapsed ? item.label : undefined}
                aria-label={item.label}
              >
                <Icon
                  size={18}
                  className={
                    active
                      ? 'text-blue-600'
                      : 'text-slate-500 group-hover:text-slate-800'
                  }
                />

                {!collapsed && <span className="truncate">{item.label}</span>}
              </button>
            )
          })}
        </div>

        {!collapsed && (
          <div className="mt-8">
            <div className="mb-3 flex items-center justify-between px-1 text-[12px] font-bold text-slate-500">
              <span>{t.shell.recentQuestions}</span>
              <button className="text-blue-600 hover:text-blue-700">
                {t.shell.viewAll}
              </button>
            </div>

            <div className="space-y-1">
              {recentQuestions.length === 0 && (
                <div className="rounded-xl border border-dashed border-slate-200 p-3 text-[12px] leading-5 text-slate-400">
                  {t.shell.noRecentQuestions}
                </div>
              )}

              {recentQuestions.map((q, index) => (
                <button
                  key={`${q.text}-${index}`}
                  className="flex w-full items-start gap-2 rounded-lg px-2 py-2 text-left text-[12px] leading-5 text-slate-600 hover:bg-slate-50 hover:text-blue-700"
                >
                  <MessageCircle
                    size={14}
                    className="mt-[2px] shrink-0 text-slate-400"
                  />
                  <span className="line-clamp-2">{q.text}</span>
                </button>
              ))}
            </div>
          </div>
        )}
      </nav>

      <div className="border-t border-slate-100 p-3">
        <button
          onClick={onNewChat}
          className={`flex h-10 w-full items-center justify-center gap-2 rounded-xl border border-blue-200 bg-blue-50 text-[13px] font-bold text-blue-700 transition hover:bg-blue-100 ${
            collapsed ? 'px-0' : 'px-3'
          }`}
          title={collapsed ? t.shell.newChat : undefined}
          aria-label={t.shell.newChat}
        >
          <PlusCircle size={17} />
          {!collapsed && <span>{t.shell.newChat}</span>}
        </button>

        <button
          onClick={() => onNavigate('settings')}
          className={`mt-2 flex h-10 w-full items-center justify-center gap-2 rounded-xl text-[13px] font-semibold text-slate-500 transition hover:bg-slate-100 hover:text-slate-900 ${
            activeView === 'settings' ? 'bg-slate-100 text-slate-900' : ''
          }`}
          title={collapsed ? t.shell.settings : undefined}
          aria-label={t.shell.settings}
        >
          <Settings size={17} />
          {!collapsed && <span>{t.shell.settings}</span>}
        </button>
      </div>
    </aside>
  )
}

function TopBar({
  onSearch,
  onSearchResults,
  rightOpen,
  onToggleRight,
}) {
  const t = useT(STR)
  const [q, setQ] = useState('')
  const [searching, setSearching] = useState(false)

  const submitKeywordSearch = async () => {
    const clean = q.trim()
    if (!clean) return

    setSearching(true)

    try {
      const results = await onSearch?.(clean)

      if (onSearchResults) {
        onSearchResults({
          query: clean,
          results: Array.isArray(results) ? results : [],
        })
      }
    } finally {
      setSearching(false)
    }
  }

  return (
    <header className="flex h-[70px] shrink-0 items-center gap-4 border-b border-slate-200 bg-white px-5">
      <div className="flex min-w-0 flex-1 items-center">
        <div className="flex h-11 w-full max-w-[760px] items-center rounded-xl border border-slate-300 bg-white shadow-sm transition focus-within:border-blue-500 focus-within:ring-4 focus-within:ring-blue-50">
          <Search size={18} className="ml-4 shrink-0 text-slate-400" />

          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') submitKeywordSearch()
            }}
            placeholder={t.topbar.placeholder}
            className="h-full min-w-0 flex-1 bg-transparent px-3 text-[14px] font-medium text-slate-800 outline-none placeholder:text-slate-400"
            aria-label={t.topbar.keywordSearch}
          />

          <button
            onClick={submitKeywordSearch}
            disabled={searching || !q.trim()}
            className="mr-1.5 inline-flex items-center gap-2 rounded-lg bg-blue-600 px-5 py-2 text-[13px] font-extrabold text-white shadow-sm hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
            title={t.topbar.keywordSearch}
            aria-label={t.topbar.keywordSearch}
          >
            {searching && <Loader2 size={14} className="animate-spin" />}
            <span>{searching ? t.topbar.searching : t.topbar.searchDocs}</span>
          </button>
        </div>
      </div>

      <div className="ml-auto flex items-center gap-2">
        <ToolbarButton icon={Clock3} label={t.shell.history} />
        <ToolbarButton icon={Bookmark} label={t.shell.bookmark} />

        <div className="mx-1 h-7 w-px bg-slate-200" />

        <LangToggle />

        <button
          onClick={onToggleRight}
          className={`grid h-9 w-9 place-items-center rounded-xl border transition ${
            rightOpen
              ? 'border-blue-200 bg-blue-50 text-blue-700'
              : 'border-slate-200 bg-white text-slate-500 hover:bg-slate-50'
          }`}
          title={rightOpen ? t.shell.collapseDocuments : t.shell.showDocuments}
          aria-label={rightOpen ? t.shell.collapseDocuments : t.shell.showDocuments}
        >
          {rightOpen ? <ChevronRight size={18} /> : <ChevronLeft size={18} />}
        </button>

        <button
          className="grid h-9 w-9 place-items-center rounded-full border border-slate-200 bg-white text-blue-700 hover:bg-blue-50"
          title={t.shell.profile}
          aria-label={t.shell.profile}
        >
          <UserCircle size={22} />
        </button>
      </div>
    </header>
  )
}

function ToolbarButton({ icon: Icon, label }) {
  return (
    <button
      className="hidden items-center gap-2 rounded-xl px-3 py-2 text-[13px] font-semibold text-slate-600 hover:bg-slate-100 hover:text-slate-950 lg:flex"
      title={label}
      aria-label={label}
    >
      <Icon size={17} />
      <span>{label}</span>
    </button>
  )
}

function RightDocumentRail({
  mode,
  library,
  workspace,
  onModeChange,
  onOpenNode,
  onOpenFullDoc,
}) {
  return (
    <aside className="h-full min-h-0 w-[360px] shrink-0 border-l border-line bg-[#f8fafc]">
      <DocSidebar
        mode={mode}
        library={library}
        activeTabId={workspace?.id}
        onModeChange={onModeChange}
        onOpenNode={onOpenNode}
        onOpenFullDoc={onOpenFullDoc}
      />
    </aside>
  )
}

function ChatCenter({ children }) {
  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-gradient-to-b from-white to-[#f8fbff]">
      <div className="mx-auto flex h-full min-h-0 w-full max-w-[860px] flex-col px-6 py-6">
        <div className="flex min-h-0 flex-1 flex-col">
          {children}
        </div>
      </div>
    </div>
  )
}

function SearchResultsCenter({
  query,
  results = [],
  loading = false,
  onOpenNode,
}) {
  const t = useT(STR)

  if (loading) {
    return (
      <div className="grid h-full place-items-center bg-gradient-to-b from-white to-[#f8fbff] px-6">
        <div className="flex items-center gap-2 rounded-2xl border border-slate-200 bg-white px-5 py-4 text-[14px] font-bold text-slate-600 shadow-sm">
          <Loader2 size={18} className="animate-spin text-blue-600" />
          <span>{t.topbar.searching}</span>
        </div>
      </div>
    )
  }

  if (!query) {
    return (
      <PlaceholderPage
        icon={Search}
        title={t.pages.searchIdleTitle}
        text={t.pages.searchIdleText}
      />
    )
  }

  if (!results.length) {
    return (
      <PlaceholderPage
        icon={Search}
        title={t.pages.searchEmptyTitle}
        text={t.pages.searchEmptyText(query)}
      />
    )
  }

  return (
    <div className="h-full overflow-y-auto bg-gradient-to-b from-white to-[#f8fbff]">
      <div className="mx-auto max-w-[960px] px-6 py-6">
        <PageHeader
          icon={Search}
          title={t.pages.searchTitle}
          text={t.pages.searchText(query, results.length)}
          aside={t.searchResults.resultCount(results.length)}
        />

        <div className="grid gap-3">
          {results.map((node) => (
            <SearchResultCard
              key={node.id}
              node={node}
              onOpenNode={onOpenNode}
            />
          ))}
        </div>
      </div>
    </div>
  )
}

function SearchResultCard({ node, onOpenNode }) {
  const t = useT(STR)

  const title =
    node?.title ||
    node?.entity ||
    node?.name ||
    node?.id ||
    t.untitled

  const summary =
    node?.summary ||
    node?.abstract ||
    node?.text ||
    node?.markdown ||
    t.searchResults.noSummary

  const source =
    node?.document ||
    node?.source ||
    node?.source_name ||
    node?.sourceName ||
    t.searchResults.unknownSource

  const typeLabel =
    node?.type === 'exogenous'
      ? t.searchResults.agentNote
      : t.searchResults.sourceNote

  return (
    <button
      onClick={() => onOpenNode?.(node)}
      className="w-full rounded-2xl border border-slate-200 bg-white p-4 text-left shadow-sm transition hover:border-blue-200 hover:bg-blue-50/30 hover:shadow-md"
    >
      <div className="flex items-start gap-3">
        <div className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-blue-50 text-blue-700">
          <FileText size={20} />
        </div>

        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <h3 className="line-clamp-1 text-[15px] font-extrabold text-slate-950">
                {title}
              </h3>

              <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] font-bold text-slate-400">
                <span>{typeLabel}</span>
                <span>·</span>
                <span className="line-clamp-1">{source}</span>
              </div>
            </div>

            <span className="shrink-0 rounded-lg border border-blue-200 bg-blue-50 px-3 py-1.5 text-[12px] font-extrabold text-blue-700">
              {t.searchResults.open}
            </span>
          </div>

          <p className="mt-3 line-clamp-3 text-[13px] leading-6 text-slate-600">
            {summary}
          </p>
        </div>
      </div>
    </button>
  )
}

function UploadCenter({ children }) {
  const t = useT(STR)

  return (
    <div className="h-full overflow-y-auto bg-gradient-to-b from-white to-[#f8fbff]">
      <div className="mx-auto max-w-[920px] px-6 py-6">
        <PageHeader
          icon={Upload}
          title={t.pages.uploadTitle}
          text={t.pages.uploadText}
        />
        {children}
      </div>
    </div>
  )
}

function SettingsCenter({ children }) {
  const t = useT(STR)

  return (
    <div className="h-full overflow-y-auto bg-gradient-to-b from-white to-[#f8fbff]">
      <div className="mx-auto max-w-[920px] px-6 py-6">
        <PageHeader
          icon={Settings}
          title={t.pages.settingsTitle}
          text={t.pages.settingsText}
        />
        {children}
      </div>
    </div>
  )
}

function MarkdownWorkspaceFrame({ item, children, onClose, onDoubleClick }) {
  const t = useT(STR)

  return (
    <div className="flex h-full flex-col bg-white">
      <div className="flex h-[54px] shrink-0 items-center gap-3 border-b border-slate-200 bg-white px-4">
        <div className="grid h-9 w-9 place-items-center rounded-xl bg-blue-50 text-blue-700">
          <FileText size={18} />
        </div>

        <div className="min-w-0 flex-1">
          <div className="truncate text-[14px] font-extrabold text-slate-950">
            {item?.doc?.title || item?.title || t.markdownFrame.fallbackTitle}
          </div>
          <div className="truncate text-[11px] font-medium text-slate-400">
            {t.markdownFrame.hint}
          </div>
        </div>

        {item?.busy && (
          <div className="mr-2 flex items-center gap-2 rounded-full bg-blue-50 px-3 py-1.5 text-[12px] font-bold text-blue-700">
            <Loader2 size={14} className="animate-spin" />
            <span>{item.busyMessage || t.markdownFrame.working}</span>
          </div>
        )}

        <button
          onClick={onClose}
          className="flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-3 py-2 text-[12px] font-bold text-slate-600 hover:bg-slate-50 hover:text-slate-950"
          title={t.markdownFrame.collapseTitle}
          aria-label={t.markdownFrame.collapseTitle}
        >
          <Minimize2 size={16} />
          <span>{t.markdownFrame.collapse}</span>
        </button>
      </div>

      <div
        onDoubleClick={onDoubleClick}
        className="min-h-0 flex-1 overflow-hidden bg-white"
      >
        {children}
      </div>
    </div>
  )
}

function PlaceholderPage({ icon: Icon = Sparkles, title, text }) {
  return (
    <div className="grid h-full place-items-center bg-gradient-to-b from-white to-[#f8fbff] px-6">
      <div className="max-w-[460px] rounded-2xl border border-dashed border-slate-300 bg-white p-8 text-center shadow-sm">
        <div className="mx-auto grid h-12 w-12 place-items-center rounded-2xl bg-blue-50 text-blue-700">
          <Icon size={24} />
        </div>
        <h1 className="mt-4 text-[22px] font-extrabold tracking-tight text-slate-950">
          {title}
        </h1>
        <p className="mt-2 text-[14px] leading-6 text-slate-500">
          {text}
        </p>
      </div>
    </div>
  )
}

function PageHeader({ icon: Icon, title, text, aside }) {
  return (
    <div className="mb-5 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex items-center gap-3">
        <div className="grid h-11 w-11 shrink-0 place-items-center rounded-2xl bg-blue-50 text-blue-700">
          <Icon size={22} />
        </div>

        <div className="min-w-0 flex-1">
          <h1 className="truncate text-[20px] font-extrabold tracking-tight text-slate-950">
            {title}
          </h1>
          <p className="mt-1 text-[13px] leading-5 text-slate-500">
            {text}
          </p>
        </div>

        {aside && (
          <div className="shrink-0 rounded-full border border-blue-200 bg-blue-50 px-3 py-1.5 text-[12px] font-extrabold text-blue-700">
            {aside}
          </div>
        )}
      </div>
    </div>
  )
}

function Centered({ children }) {
  return (
    <div className="grid h-full place-items-center bg-white text-[14px] text-slate-500">
      {children}
    </div>
  )
}

function AppFooter() {
  const t = useT(STR)

  return (
    <footer className="flex h-[42px] shrink-0 items-center justify-between border-t border-slate-200 bg-white px-5 text-[12px] font-medium text-slate-400">
      <span>{t.shell.footerCopyright('2024')}</span>

      <div className="flex items-center gap-5">
        <button className="hover:text-slate-700">
          {t.shell.terms}
        </button>
        <button className="hover:text-slate-700">
          {t.shell.privacy}
        </button>
      </div>
    </footer>
  )
}


const pdfApiBase = 'http://localhost:51025'

function describeWriteJob(job, doneText, t) {
  if (!job?.status) return t.startingWrite
  if (job.status === 'queued') {
    return job.position ? t.queuedPos(job.position) : t.queued
  }
  if (job.status === 'running') return t.writing
  if (job.status === 'done') return doneText ?? t.writeFinished
  if (job.status === 'failed') return job.error || t.writeFailed
  if (job.status === 'cancelled') return t.writeCancelled
  return t.statusOf(job.status)
}

export default function App() {
  const [raw, setRaw] = useState({ nodes: [], edges: [] })
  const [health, setHealth] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  /**
   * Shell state.
   *
   * centerView controls the main center area.
   * No tab bar anymore.
   */
  const [leftCollapsed, setLeftCollapsed] = useState(false)
  const [rightOpen, setRightOpen] = useState(true)
  const [rightMode, setRightMode] = useState('documents') // documents | topics
  const [centerView, setCenterView] = useState('chat') // chat | graph | upload | glossary | settings | markdown | search

  /**
   * Keyword-search state.
   *
   * The top bar only performs keyword search.
   * Agent runs happen only from ChatPanel.
   */
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState([])
  const [searchLoading, setSearchLoading] = useState(false)
  const searchSeq = useRef(0)

  /**
   * Single markdown workspace instead of tab list.
   *
   * This replaces the old tab UI while preserving the data model needed by
   * MarkdownView.
   */
  const [workspace, setWorkspace] = useState(null)

  const draftSeq = useRef(0)

  // chat
  const [messages, setMessages] = useState([])
  const [activeAnswerId, setActiveAnswerId] = useState(null)
  const [savedIds, setSavedIds] = useState(() => new Set())
  const [addingIds, setAddingIds] = useState(() => new Set())
  const [answerWriteStatuses, setAnswerWriteStatuses] = useState(() => new Map())
  const answerSeq = useRef(0)

  const [focusIds, setFocusIds] = useState(null)
  const [toast, setToast] = useState(null)
  const [assimPending, setAssimPending] = useState(0)

  // Per-request agent overrides.
  const [overrides, setOverrides] = useState(() => {
    try {
      return JSON.parse(window.localStorage.getItem('wikiOverrides') || 'null')
    } catch {
      return null
    }
  })

  const applyOverrides = (o) => {
    setOverrides(o)
    window.localStorage.setItem('wikiOverrides', JSON.stringify(o))
  }

  const t = useT(STR)

  const rawById = useMemo(() => new Map(raw.nodes.map((n) => [n.id, n])), [raw])

  const openIds = useMemo(() => {
    if (workspace?.kind === 'doc' && workspace.nodeId) {
      return new Set([workspace.nodeId])
    }

    return new Set()
  }, [workspace])

  const answerIds = useMemo(() => {
    if (workspace?.kind === 'answer') {
      return new Set(workspace.answer?.citedIds || [])
    }

    return new Set()
  }, [workspace])

  const graph = useMemo(() => layoutGraph(raw.nodes, raw.edges), [raw])

  /**
   * Document library for the right rail.
   *
   * IMPORTANT:
   * Use raw.nodes/raw.edges here, not graph.nodes/graph.edges.
   * `graph` is layout data for the canvas.
   * `raw` is the original backend graph data needed by buildLibrary().
   */
  const docLibrary = useMemo(() => {
    const docTime = (doc) => {
      const nodes = Array.isArray(doc?.nodes) ? doc.nodes : []

      let best = 0

      for (const n of nodes) {
        const candidates = [
          n.updated_at,
          n.updatedAt,
          n.modified_at,
          n.modifiedAt,
          n.created_at,
          n.createdAt,
          n.timestamp,
          n.time,
        ]

        for (const value of candidates) {
          const parsed =
            typeof value === 'number'
              ? value
              : value
                ? Date.parse(value)
                : 0

          if (Number.isFinite(parsed) && parsed > best) {
            best = parsed
          }
        }
      }

      return best
    }

    const sortDocumentsForSidebar = (a, b) => {
      const aTime = docTime(a)
      const bTime = docTime(b)

      if (aTime !== bTime) return bTime - aTime

      return String(a.name || '').localeCompare(String(b.name || ''))
    }

    const sortTopicsForSidebar = (a, b) => {
      const aCount = Array.isArray(a.docs) ? a.docs.length : 0
      const bCount = Array.isArray(b.docs) ? b.docs.length : 0

      if (aCount !== bCount) return bCount - aCount

      return String(a.cluster || '').localeCompare(String(b.cluster || ''))
    }

    return buildLibrary(raw.nodes || [], raw.edges || [], {
      sortDocuments: sortDocumentsForSidebar,
      sortTopicDocuments: sortDocumentsForSidebar,
      sortTopics: sortTopicsForSidebar,
    })
  }, [raw.nodes, raw.edges])

  const fireToast = (text) => {
    setToast(text)
    setTimeout(() => setToast(null), 3600)
  }

  const reload = useCallback(async () => {
    const [g, h] = await Promise.all([api.graph(), api.health()])

    setRaw({
      nodes: g.nodes,
      edges: g.edges,
    })

    setHealth(h)

    return g
  }, [])

  useEffect(() => {
    reload()
      .catch((e) => setError(String(e.message || e)))
      .finally(() => setLoading(false))
  }, [reload])

  // Poll background-assimilation backlog.
  useEffect(() => {
    let alive = true

    const poll = async () => {
      try {
        const res = await api.assimilation()

        if (alive) {
          setAssimPending(res?.pending ?? 0)
        }
      } catch {
        // endpoint optional
      }
    }

    poll()

    const timer = setInterval(poll, 4000)

    return () => {
      alive = false
      clearInterval(timer)
    }
  }, [])

  const setAnswerWriteStatus = (answerId, text) => {
    if (!answerId) return

    setAnswerWriteStatuses((prev) => {
      const next = new Map(prev)

      if (text) {
        next.set(answerId, text)
      } else {
        next.delete(answerId)
      }

      return next
    })
  }

  // ---------------------------------------------------------------------------
  // Workspace helpers
  // ---------------------------------------------------------------------------

  const updateWorkspace = (id, patch) => {
    setWorkspace((prev) => {
      if (!prev || prev.id !== id) return prev

      return {
        ...prev,
        ...patch,
      }
    })
  }

  const closeWorkspace = () => {
    setWorkspace(null)
    setCenterView('chat')
  }

  const openWorkspace = (next) => {
    setWorkspace(next)
    setCenterView('markdown')
  }

  const openNode = (n) => {
    const built = docFromNode(n)
    const id = `doc:${n.id}`

    openWorkspace({
      id,
      kind: 'doc',
      nodeId: n.id,
      title: built.title,
      doc: built,
      draft: {
        title: built.title,
        markdown: built.markdown,
      },
      editing: false,
      busy: false,
      busyMessage: '',
    })

    setFocusIds(new Set([n.id]))
  }

  const openNodeById = async (id) => {
    try {
      const node = await api.node(id)
      openNode(node)
    } catch (e) {
      fireToast(t.couldNotOpen(e.message))
    }
  }

  const openSearchResult = async (nodeOrId) => {
    if (!nodeOrId) return

    if (typeof nodeOrId === 'string') {
      await openNodeById(nodeOrId)
      return
    }

    if (nodeOrId.id) {
      await openNodeById(nodeOrId.id)
      return
    }

    openNode(nodeOrId)
  }

  const openFullDoc = (doc) => {
    const { markdown, positions } = reconstructDocument(doc)

    const id = `fulldoc:${doc.name}`

    const built = {
      title: doc.name,
      badge: doc.type === 'exogenous' ? t.agentNote : t.sourceNote,
      meta: t.fullDocMeta(doc.nodes.length),
      markdown,
    }

    openWorkspace({
      id,
      kind: 'fulldoc',
      title: doc.name,
      doc: built,
      draft: {
        title: built.title,
        markdown,
      },
      positions,
      editing: false,
      busy: false,
      busyMessage: '',
    })

    setFocusIds(new Set(doc.nodes.map((n) => n.id)))
  }

  const openDraft = ({
    title,
    filename,
    markdown,
    sourceType = 'endogenous',
    sourcePath,
    sourceRanges,
  }) => {
    draftSeq.current += 1

    const id = `draft:${draftSeq.current}`
    const draftTitle = title || filename || t.untitled
    const badge = sourceType === 'exogenous' ? t.agentNote : t.sourceNote

    const built = {
      title: draftTitle,
      badge,
      meta: t.unsavedDraft,
      markdown,
    }

    openWorkspace({
      id,
      kind: 'draft',
      title: draftTitle,
      sourceType,
      sourceName: filename || draftTitle,
      sourcePath,
      sourceRanges,
      doc: built,
      draft: {
        title: draftTitle,
        markdown,
      },
      editing: false,
      busy: false,
      busyMessage: '',
    })
  }

  /**
   * Answers are stored as the current workspace, but by default we do not
   * force navigation away from chat when an answer arrives.
   *
   * - Generated answer: stays in chat.
   * - User clicks "view answer": opens full markdown workspace.
   */
  const openAnswerTab = (answer, expand = true) => {
    if (!answer) return

    const id = `answer:${answer.id}`
    const title = answer.title || answer.question || t.answer
    const markdown = answer.markdown || ''

    const next = {
      id,
      kind: 'answer',
      title: title.slice(0, 28),
      sourceType: 'exogenous',
      sourceIds: answer.citedIds || [],
      sourceName: title,
      answer,
      doc: {
        title,
        badge: t.agentNote,
        meta: answer.steps ? t.answerMetaSteps(answer.steps) : t.answerMeta,
        markdown,
      },
      draft: {
        title,
        markdown,
      },
      editing: false,
      busy: false,
      busyMessage: '',
      refs: answer.refs || [],
    }

    setWorkspace(next)
    setActiveAnswerId(answer.id)
    setFocusIds(new Set(answer.citedIds || []))

    if (expand) {
      setCenterView('markdown')
    }
  }

  const startEdit = (id) => updateWorkspace(id, { editing: true })

  const changeTitle = (id, title) => {
    setWorkspace((prev) => {
      if (!prev || prev.id !== id) return prev

      return {
        ...prev,
        draft: {
          ...prev.draft,
          title,
        },
      }
    })
  }

  const changeBody = (id, markdown) => {
    setWorkspace((prev) => {
      if (!prev || prev.id !== id) return prev

      return {
        ...prev,
        draft: {
          ...prev.draft,
          markdown,
        },
      }
    })
  }

  // ---------------------------------------------------------------------------
  // Document editing / graph writes
  // ---------------------------------------------------------------------------

  const saveNode = async (item) => {
    updateWorkspace(item.id, {
      busy: true,
      busyMessage: t.startingUpdate,
    })

    try {
      const node = await api.updateNode(item.nodeId, item.draft.markdown, {
        onProgress: (job) => {
          updateWorkspace(item.id, {
            busyMessage: describeWriteJob(job, t.nodeUpdated, t),
          })
        },
      })

      await reload()

      const built = docFromNode(node)
      const nextId = `doc:${node.id}`

      setWorkspace((prev) => {
        if (!prev || prev.id !== item.id) return prev

        return {
          ...prev,
          id: nextId,
          nodeId: node.id,
          title: built.title,
          doc: built,
          draft: {
            title: built.title,
            markdown: built.markdown,
          },
          editing: false,
          busy: false,
          busyMessage: '',
        }
      })

      setFocusIds(new Set([node.id]))
      fireToast(t.savedNodeUpdated)
    } catch (e) {
      updateWorkspace(item.id, {
        busy: false,
        busyMessage: '',
      })

      fireToast(t.updateFailed(e.message))
    }
  }

  const deleteNode = async (item) => {
    updateWorkspace(item.id, {
      busy: true,
      busyMessage: t.startingDelete,
    })

    try {
      await api.deleteNode(item.nodeId, {
        onProgress: (job) => {
          updateWorkspace(item.id, {
            busyMessage: describeWriteJob(job, t.nodeDeleted, t),
          })
        },
      })

      await reload()
      closeWorkspace()
      fireToast(t.noteDeleted)
    } catch (e) {
      updateWorkspace(item.id, {
        busy: false,
        busyMessage: '',
      })

      fireToast(t.deleteFailed(e.message))
    }
  }

  const uploadMarkdown = async ({ filename, markdown }, onStatus) => {
    try {
      onStatus?.(t.startingWriteGraph)

      const node = await api.createDocument(
        {
          body: markdown,
          title: (filename || t.untitled).replace(/\.(md|markdown)$/i, ''),
          documentName: filename,
        },
        {
          onProgress: (job) => onStatus?.(describeWriteJob(job, t.mdAdded, t)),
          onAssimilating: (msg) => fireToast(msg),
        },
      )

      await reload()
      openNode(node)

      onStatus?.(t.mdAddedOpened)
      fireToast(t.mdAdded)

      return node
    } catch (e) {
      fireToast(t.addFailed(e.message))
      throw e
    }
  }

  const addDraftToGraph = async (item) => {
    updateWorkspace(item.id, {
      busy: true,
      busyMessage: t.startingWriteGraph,
    })

    if (item.answer?.id) {
      setAddingIds((prev) => new Set(prev).add(item.answer.id))
      setAnswerWriteStatus(item.answer.id, t.startingWriteGraph)
    }

    try {
      const node =
        item.sourceType === 'exogenous'
          ? await api.createExogenous(
              item.draft.markdown,
              item.sourceIds || [],
              `${item.kind === 'answer' ? 'agent' : 'human'}:${item.draft.title.slice(0, 60)}`,
              {
                onProgress: (job) => {
                  const text = describeWriteJob(job, t.addedToGraph, t)

                  updateWorkspace(item.id, {
                    busyMessage: text,
                  })

                  if (item.answer?.id) {
                    setAnswerWriteStatus(item.answer.id, text)
                  }
                },
                onAssimilating: (msg) => fireToast(msg),
              },
            )
          : await api.createDocument(
              {
                body: item.draft.markdown,
                title: item.draft.title,
                documentName: item.sourceName || item.draft.title,
                sourcePath: item.sourcePath,
                sourceRanges: item.sourceRanges,
              },
              {
                onProgress: (job) => {
                  updateWorkspace(item.id, {
                    busyMessage: describeWriteJob(job, t.addedToGraph, t),
                  })
                },
                onAssimilating: (msg) => fireToast(msg),
              },
            )

      await reload()

      if (item.answer?.id) {
        setSavedIds((prev) => new Set(prev).add(item.answer.id))
        setAnswerWriteStatus(item.answer.id, t.savedOpened)
      }

      openNode(node)
      fireToast(t.addedToGraph)
    } catch (e) {
      updateWorkspace(item.id, {
        busy: false,
        busyMessage: '',
      })

      if (item.answer?.id) {
        setAnswerWriteStatus(item.answer.id, '')
      }

      fireToast(t.addFailed(e.message))
    } finally {
      if (item.answer?.id) {
        setAddingIds((prev) => {
          const next = new Set(prev)
          next.delete(item.answer.id)
          return next
        })
      }
    }
  }

  // ---------------------------------------------------------------------------
  // Chat / agent
  // ---------------------------------------------------------------------------

  const refLabel = (id) => {
    const n = rawById.get(id)

    return {
      id,
      label: n?.title || n?.entity || id,
      note: n?.type === 'exogenous' ? t.agentNote : t.sourceNote,
    }
  }

  const patchLast = (fn) => {
    setMessages((prev) => {
      const copy = prev.slice()
      const i = copy.length - 1

      if (i >= 0 && copy[i].role === 'assistant') {
        copy[i] = fn(copy[i])
      }

      return copy
    })
  }

  const activityLine = (ev) => {
    const who = ev.agent ? t.explorer(ev.agent) : null
    const nm = (n) => n?.title || n?.id || '…'

    switch (ev.type) {
      case 'search':
        return t.searching(who, ev.query)
      case 'candidates':
        return t.pagesFound(ev.count)
      case 'subagents_spawned':
        return t.spawned(ev.starts?.length || 0)
      case 'subagent_start':
        return t.exploring(who, nm(ev.node))
      case 'read':
        return t.reading(who, nm(ev.node))
      case 'follow_link':
        return t.following(who, nm(ev.node), ev.neighbors)
      case 'subagent_done':
        return t.subDone(who, ev.cited?.length || 0)
      case 'compiling':
        return t.compiling
      case 'diagram_pending':
        return t.diagramBuilding
      case 'diagram_ready':
        return t.diagramReady
      case 'diagram_failed':
        return t.diagramFailed
      default:
        return null
    }
  }

  const finalizeAnswer = (ans, activity, q) => {
    const cited = ans.cited_node_ids || []
    const refs = cited.map(refLabel)
    const hasAnswer = !!(ans.answer && ans.answer.trim())

    setFocusIds(new Set(cited))

    if (hasAnswer) {
      answerSeq.current += 1

      const id = answerSeq.current

      const answer = {
        id,
        question: q,
        title: q,
        markdown: ans.answer,
        refs,
        steps: ans.steps,
        citedIds: cited,
      }

      patchLast(() => ({
        role: 'assistant',
        title: t.answerReady(ans.steps),
        text: ans.answer,
        activity,
        answer,
      }))

      // Store as workspace data, but do not force full markdown view.
      openAnswerTab(answer, false)
    } else {
      patchLast(() => ({
        role: 'assistant',
        title: t.foundNoBody,
        text: t.foundNoBodyText(cited.length, ans.steps),
        refs,
        activity,
      }))

      if (cited[0]) {
        openNodeById(cited[0])
      }
    }
  }

  const ask = async (q) => {
    const clean = q.trim()

    if (!clean) return

    setCenterView('chat')

    setMessages((prev) => [
      ...prev,
      {
        role: 'user',
        text: clean,
      },
      {
        role: 'assistant',
        streaming: true,
        title: t.working,
        activity: [],
      },
    ])

    const activity = []

    try {
      await api.askStream(clean, overrides, (ev) => {
        if (ev.type === 'answer') {
          return finalizeAnswer(ev, activity, clean)
        }

        if (ev.type === 'error') {
          return patchLast(() => ({
            role: 'assistant',
            title: t.requestFailed,
            text: ev.message,
          }))
        }

        if (ev.type === 'diagram_pending') {
          patchLast((m) => ({
            ...m,
            _diagState: 'pending',
          }))
        } else if (ev.type === 'diagram_ready') {
          patchLast((m) => ({
            ...m,
            _diagState: 'ready',
            _diagMd: ev.answer ?? m._diagMd,
          }))
        } else if (ev.type === 'diagram_failed') {
          patchLast((m) => ({
            ...m,
            _diagState: 'failed',
            _diagMd: ev.answer ?? m._diagMd,
          }))
        }

        const line = activityLine(ev)

        if (!line) return

        activity.push(line)

        patchLast((m) => ({
          ...m,
          activity: [...activity],
        }))
      })
    } catch (e) {
      patchLast(() => ({
        role: 'assistant',
        title: t.requestFailed,
        text: e.message,
      }))
    }
  }

  const addWiki = async (answer) => {
    if (!answer || savedIds.has(answer.id) || addingIds.has(answer.id)) return

    const answerWorkspace =
      workspace?.kind === 'answer' && workspace.answer?.id === answer.id
        ? workspace
        : null

    const markdown = answerWorkspace?.draft?.markdown ?? answer.markdown

    const title =
      answerWorkspace?.draft?.title ??
      answer.title ??
      answer.question ??
      t.answer

    const citedIds = answerWorkspace?.sourceIds || answer.citedIds || []

    setAddingIds((prev) => new Set(prev).add(answer.id))
    setAnswerWriteStatus(answer.id, t.startingWriteGraph)

    if (answerWorkspace) {
      updateWorkspace(answerWorkspace.id, {
        busy: true,
        busyMessage: t.startingWriteGraph,
      })
    }

    try {
      const node = await api.createExogenous(
        markdown,
        citedIds,
        `agent:${title.slice(0, 60)}`,
        {
          onProgress: (job) => {
            const text = describeWriteJob(job, t.savedToGraph, t)

            setAnswerWriteStatus(answer.id, text)

            if (answerWorkspace) {
              updateWorkspace(answerWorkspace.id, {
                busyMessage: text,
              })
            }
          },
          onAssimilating: (msg) => fireToast(msg),
        },
      )

      await reload()

      setSavedIds((prev) => new Set(prev).add(answer.id))
      setAnswerWriteStatus(answer.id, t.savedOpened)

      openNode(node)
      setFocusIds(new Set([node.id, ...citedIds]))

      fireToast(t.savedWikiNote)
    } catch (e) {
      setAnswerWriteStatus(answer.id, '')

      if (answerWorkspace) {
        updateWorkspace(answerWorkspace.id, {
          busy: false,
          busyMessage: '',
        })
      }

      fireToast(t.saveFailed(e.message))
    } finally {
      setAddingIds((prev) => {
        const next = new Set(prev)
        next.delete(answer.id)
        return next
      })
    }
  }

  /**
   * Keyword search only.
   *
   * Important:
   * - Does not run the agent.
   * - Does not auto-open the first result.
   * - Returns multiple nodes to TopBar/SearchResultsCenter.
   */
  const onSearch = async (text) => {
    const clean = text.trim()

    if (!clean) return []

    searchSeq.current += 1

    const seq = searchSeq.current

    setSearchQuery(clean)
    setSearchResults([])
    setSearchLoading(true)
    setCenterView('search')

    try {
      const results = await api.search(clean, 12)
      const list = Array.isArray(results) ? results : []

      if (seq === searchSeq.current) {
        setSearchResults(list)
      }

      return list
    } catch (e) {
      if (seq === searchSeq.current) {
        setSearchResults([])
      }

      fireToast(t.searchFailed(e.message))

      return []
    } finally {
      if (seq === searchSeq.current) {
        setSearchLoading(false)
      }
    }
  }

  // ---------------------------------------------------------------------------
  // Navigation
  // ---------------------------------------------------------------------------

  const handleNav = (view) => {
    if (view === 'topics') {
      setRightMode('topics')
      setRightOpen(true)
      return
    }

    if (view === 'documents') {
      setRightMode('documents')
      setRightOpen(true)
      return
    }

    setCenterView(view)
  }

  const handleNewChat = () => {
    setMessages([])
    setWorkspace(null)
    setActiveAnswerId(null)
    setCenterView('chat')
  }

  const recentQuestions = useMemo(() => {
    return messages
      .filter((m) => m.role === 'user')
      .slice(-5)
      .reverse()
  }, [messages])

  const renderCenter = () => {
    if (loading) {
      return <Centered>{t.loadingGraph}</Centered>
    }

    if (error) {
      return (
        <Centered>
          <div className="max-w-[420px] text-center">
            <p className="font-bold text-red">{t.cannotReach}</p>
            <p className="mt-2 text-[13px] text-muted">{error}</p>
          </div>
        </Centered>
      )
    }

    if (centerView === 'graph') {
      return (
        <div className="h-full bg-white">
          <GraphCanvas
            nodes={graph.nodes}
            edges={graph.edges}
            clusters={graph.clusters}
            worldW={graph.worldW}
            worldH={graph.worldH}
            openIds={openIds}
            answerIds={answerIds}
            showConflict={false}
            showStale={false}
            onOpenNode={openNode}
          />
        </div>
      )
    }

    if (centerView === 'search') {
      return (
        <SearchResultsCenter
          query={searchQuery}
          results={searchResults}
          loading={searchLoading}
          onOpenNode={openSearchResult}
        />
      )
    }

    if (centerView === 'upload') {
      return (
        <UploadCenter>
          <UploadView
            pdfApiBase={pdfApiBase}
            onOpenDraft={openDraft}
            onUploadMarkdown={uploadMarkdown}
          />
        </UploadCenter>
      )
    }

    if (centerView === 'glossary') {
      return (
        <PlaceholderPage
          icon={BookMarked}
          title={t.pages.glossaryTitle}
          text={t.pages.glossaryText}
        />
      )
    }

    if (centerView === 'settings') {
      return (
        <SettingsCenter>
          <SettingsView overrides={overrides} onApply={applyOverrides} />
        </SettingsCenter>
      )
    }

    if (centerView === 'markdown' && workspace) {
      return (
        <MarkdownWorkspaceFrame
          item={workspace}
          onClose={closeWorkspace}
          onDoubleClick={() => {
            if (workspace.kind !== 'fulldoc') {
              startEdit(workspace.id)
            }
          }}
        >
          <MarkdownView
            doc={workspace.doc}
            draft={workspace.draft}
            isEditing={!!workspace.editing}
            dirty={
              workspace.kind !== 'fulldoc' &&
              (
                workspace.draft.markdown !== workspace.doc.markdown ||
                workspace.draft.title !== workspace.doc.title
              )
            }
            mode={workspace.kind}
            positions={workspace.positions}
            busy={!!workspace.busy}
            busyMessage={workspace.busyMessage}
            refs={workspace.refs}
            onStartEdit={() => startEdit(workspace.id)}
            onConfirm={() => {
              if (workspace.kind === 'doc') {
                saveNode(workspace)
              } else {
                addDraftToGraph(workspace)
              }
            }}
            onDelete={() => {
              if (workspace.kind === 'doc') {
                deleteNode(workspace)
              } else {
                closeWorkspace()
              }
            }}
            onAddToGraph={() => addDraftToGraph(workspace)}
            onOpenNode={openNodeById}
            onChangeTitle={(v) => changeTitle(workspace.id, v)}
            onChangeBody={(v) => changeBody(workspace.id, v)}
          />
        </MarkdownWorkspaceFrame>
      )
    }

    return (
      <div className="flex h-full min-h-0 flex-col overflow-hidden bg-gradient-to-b from-white to-[#f8fbff]">
        <div className="flex h-full min-h-0 w-full flex-col px-0 pt-6 pb-0">
          <div className="flex min-h-0 flex-1 flex-col">
            <ChatPanel
              messages={messages}
              health={health}
              onAsk={ask}
              onOpenNode={openNodeById}
              onAddWiki={addWiki}
              onViewAnswer={(answer) => openAnswerTab(answer, true)}
              activeAnswerId={activeAnswerId}
              savedIds={savedIds}
              addingIds={addingIds}
              writeStatuses={answerWriteStatuses}
            />
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-[#f6f8fc] text-slate-900">
      <LeftSidebar
        collapsed={leftCollapsed}
        activeView={centerView}
        rightMode={rightMode}
        recentQuestions={recentQuestions}
        onToggle={() => setLeftCollapsed((v) => !v)}
        onNavigate={handleNav}
        onNewChat={handleNewChat}
      />

      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar
          onSearch={onSearch}
          onSearchResults={({ query, results }) => {
            setSearchQuery(query)
            setSearchResults(Array.isArray(results) ? results : [])
            setSearchLoading(false)
            setCenterView('search')
          }}
          rightOpen={rightOpen}
          onToggleRight={() => setRightOpen((v) => !v)}
        />

        <div className="relative flex min-h-0 flex-1 overflow-hidden">
          <main className="relative min-w-0 flex-1 overflow-hidden bg-white">
            <ErrorBoundary resetKey={`${centerView}:${workspace?.id || 'none'}:${searchQuery}`}>
              {renderCenter()}
            </ErrorBoundary>

            {toast && (
              <div className="absolute bottom-[22px] right-[22px] z-30 max-w-[390px] rounded-xl border border-emerald-200 bg-emerald-50 px-[14px] py-[13px] text-[13px] leading-[1.45] text-emerald-800 shadow-xl">
                {toast}
              </div>
            )}

            {assimPending > 0 && (
              <div
                title={t.assimTitle}
                className="absolute bottom-[22px] left-[22px] z-30 flex items-center gap-2 rounded-full border border-blue-200 bg-blue-50 px-[12px] py-[7px] text-[12px] font-medium text-blue-800 shadow-lg"
              >
                <span className="h-[7px] w-[7px] animate-pulse rounded-full bg-blue-600" />
                {t.assimBadge} {assimPending}
              </div>
            )}
          </main>

          {rightOpen && (
            <RightDocumentRail
              mode={rightMode}
              library={docLibrary}
              workspace={workspace}
              onModeChange={setRightMode}
              onOpenNode={openNode}
              onOpenFullDoc={openFullDoc}
              onClose={() => setRightOpen(false)}
            />
          )}
        </div>

        <AppFooter />
      </div>
    </div>
  )
}

