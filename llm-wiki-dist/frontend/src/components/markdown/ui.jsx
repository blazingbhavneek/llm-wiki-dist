export function InlineSpinner() {
  return (
    <span className="inline-block h-[8px] w-[8px] animate-pulse rounded-full bg-blue" />
  )
}

export function SmallBtn({
  children,
  onClick,
  active,
  disabled,
  confirm,
  danger,
  title,
}) {
  const base = 'border px-[11px] py-[8px] text-[12px] font-bold'

  const tone = disabled
    ? 'cursor-not-allowed border-line bg-white text-muted opacity-45'
    : danger
      ? 'border-red/25 bg-red/10 text-[#7c1230] hover:bg-red/15'
      : confirm && !disabled
        ? 'border-green/25 bg-[#ecfdf5] text-[#065f46]'
        : active
          ? 'border-blue/25 bg-blue/10 text-[#244a9d]'
          : 'border-line bg-white text-muted hover:border-line2 hover:text-ink'

  return (
    <button
      type="button"
      className={`${base} ${tone}`}
      onClick={onClick}
      disabled={disabled}
      title={title}
    >
      {children}
    </button>
  )
}
