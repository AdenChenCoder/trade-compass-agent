export function Icon({ name }: { name: keyof typeof paths }) {
  return <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">{paths[name]}</svg>;
}

const paths = {
  computer: <><rect x="3" y="4" width="18" height="13" rx="2" /><path d="M8 21h8m-4-4v4" /></>,
  phone: <><rect x="6" y="2" width="12" height="20" rx="3" /><path d="M10 18h4" /></>,
  arrow: <path d="M5 12h14m-6-6 6 6-6 6" />,
  back: <path d="M19 12H5m6-6-6 6 6 6" />,
  chat: <path d="M5 4h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9l-5 3V6a2 2 0 0 1 2-2m2 5h8m-8 4h5" />,
  bell: <><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4" /></>,
  lock: <><rect x="5" y="10" width="14" height="11" rx="2" /><path d="M8 10V6a4 4 0 0 1 8 0v4m-4 4v3" /></>,
  plus: <><rect x="3" y="3" width="18" height="18" rx="5" /><path d="M8 12h8m-4-4v8" /></>,
  chevron: <path d="m9 5 7 7-7 7" />,
  robot: <><rect x="4" y="7" width="16" height="13" rx="4" /><path d="M12 3v4M1 12v4m22-4v4M9 16h6M8 11v1m8-1v1" /></>,
  person: <><circle cx="12" cy="8" r="3" /><path d="M5 20v-2a7 7 0 0 1 14 0v2" /></>,
  link: <><path d="m10 13 4-4m-5 7-2 2a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0m2-3 2-2a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0" transform="translate(1 1)" /></>,
  refresh: <><path d="M20 7v5h-5M4 17v-5h5M6 6a8 8 0 0 1 13 3M5 15a8 8 0 0 0 13 3" /></>,
  send: <path d="m21 3-7 18-4-7-7-4 18-7ZM10 14 21 3" />,
  check: <path d="m5 12 4 4L19 6" />,
  alert: <><circle cx="12" cy="12" r="9" /><path d="M12 7v6m0 4h.01" /></>,
  close: <path d="m6 6 12 12M6 18 18 6" />,
  copy: <><rect x="8" y="8" width="12" height="13" rx="2" /><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3" /></>,
  more: <><circle cx="5" cy="12" r="1" /><circle cx="12" cy="12" r="1" /><circle cx="19" cy="12" r="1" /></>,
};
