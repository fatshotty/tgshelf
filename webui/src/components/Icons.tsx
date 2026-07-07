import type { ReactNode } from 'react'

type IconProps = {
  className?: string
  title?: string
}

function SvgIcon({
  className,
  title,
  children,
}: IconProps & { children: ReactNode }) {
  return (
    <svg
      className={`svg-icon${className ? ` ${className}` : ''}`}
      viewBox="0 0 24 24"
      aria-hidden={title ? undefined : true}
      role={title ? 'img' : undefined}
      focusable="false"
    >
      {title ? <title>{title}</title> : null}
      {children}
    </svg>
  )
}

export function FolderIcon(props: IconProps) {
  return (
    <SvgIcon {...props}>
      <path d="M3 7.25A2.25 2.25 0 0 1 5.25 5h4.1c.6 0 1.17.24 1.59.66l1.09 1.09h6.72A2.25 2.25 0 0 1 21 9v7.75A2.25 2.25 0 0 1 18.75 19H5.25A2.25 2.25 0 0 1 3 16.75Z" />
      <path d="M3.25 9.25h17.5" />
    </SvgIcon>
  )
}

export function FileIcon(props: IconProps) {
  return (
    <SvgIcon {...props}>
      <path d="M6 3.75A1.75 1.75 0 0 1 7.75 2h5.75L18 6.5v13.75A1.75 1.75 0 0 1 16.25 22h-8.5A1.75 1.75 0 0 1 6 20.25Z" />
      <path d="M13.5 2.25V6.5H18" />
      <path d="M8.75 11.5h6.5" />
      <path d="M8.75 15h6.5" />
    </SvgIcon>
  )
}

export function DownloadIcon(props: IconProps) {
  return (
    <SvgIcon {...props}>
      <path d="M12 3.75v10.5" />
      <path d="m7.75 10 4.25 4.25L16.25 10" />
      <path d="M5 19.25h14" />
    </SvgIcon>
  )
}

export function MoreIcon(props: IconProps) {
  return (
    <SvgIcon {...props}>
      <circle className="icon-fill" cx="12" cy="5.75" r="1.35" />
      <circle className="icon-fill" cx="12" cy="12" r="1.35" />
      <circle className="icon-fill" cx="12" cy="18.25" r="1.35" />
    </SvgIcon>
  )
}
