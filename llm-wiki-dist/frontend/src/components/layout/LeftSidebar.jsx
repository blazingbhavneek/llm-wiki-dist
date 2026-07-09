import {
  BookMarked,
  BookOpen,
  ListTodo,
  MessageCircle,
  Network,
  PanelLeftClose,
  PanelLeftOpen,
  PlusCircle,
  Settings,
  Upload,
} from 'lucide-react'

import { useT } from '../../i18n.jsx'
import { STR } from './strings.js'

export function LeftSidebar({
  collapsed,
  activeView,
  activeRightTabId,
  rightOpen,
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
      id: 'queue',
      label: t.shell.queue,
      icon: ListTodo,
      view: 'queue',
    },
    {
      id: 'glossary',
      label: t.shell.glossary,
      icon: BookMarked,
      view: 'glossary',
    },
    // {
    //   id: 'explorer',
    //   label: t.shell.explorer,
    //   icon: FolderTree,
    //   view: 'explorer',
    // },
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
          <div className="flex w-full justify-center">
            <img
              src="/favicon.svg"
              alt="Logo"
              className="block h-[100px] w-[100px] max-w-full object-contain"
            />
          </div>

          {/* {!collapsed && (
            <div className="min-w-0 flex-1">
              <div className="truncate text-[17px] font-extrabold tracking-tight text-slate-950">
                {t.brand}
              </div>
              <div className="truncate text-[11px] font-medium text-slate-400">
                {t.brandSubtitle}
              </div>
            </div>
          )} */}
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
              item.view === 'explorer'
                ? rightOpen && activeRightTabId === 'explorer'
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

