import { useLayoutEffect, useRef, useState, type RefObject } from 'react';

export function useConversationScroll(area: RefObject<HTMLElement | null>, session: string | null,
  end: number, pendingId: string | undefined, nearBottom: RefObject<boolean>) {
  const previous = useRef({ session, end, pendingId });
  const unread = useRef<number | null>(null);
  const [away, setAway] = useState(false);
  const [hasUnread, setHasUnread] = useState(false);
  const [announcement, setAnnouncement] = useState('');
  const onScroll = () => {
    const node = area.current;
    if (!node) return;
    nearBottom.current = node.scrollHeight - node.clientHeight - node.scrollTop < 64;
    setAway(!nearBottom.current);
    if (nearBottom.current) { unread.current = null; setHasUnread(false); }
  };
  const showReply = (index: number | null, smooth = false) => {
    const node = area.current;
    if (!node) return;
    const message = index === null ? null : node.querySelector<HTMLElement>(`[data-message-index="${index}"]`);
    const available = node.clientHeight - (node.querySelector('.composer')?.clientHeight ?? 0) - 32;
    // Land at the beginning of a long answer, not its last paragraph.
    const top = message && message.getBoundingClientRect().height > available
      ? node.scrollTop + message.getBoundingClientRect().top - node.getBoundingClientRect().top - 16
      : node.scrollHeight;
    if (smooth && !matchMedia('(prefers-reduced-motion: reduce)').matches) node.scrollTo({ top, behavior: 'smooth' });
    else node.scrollTop = top;
  };
  useLayoutEffect(() => {
    const node = area.current;
    if (!session || !node || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(() => {
      if (nearBottom.current) showReply(null);
      onScroll();
    });
    observer.observe(node);
    const composer = node.querySelector('.composer');
    if (composer) observer.observe(composer);
    return () => observer.disconnect();
  }, [session]);
  useLayoutEffect(() => {
    const last = previous.current;
    previous.current = { session, end, pendingId };
    if (!session) { unread.current = null; setAway(false); setHasUnread(false); setAnnouncement(''); return; }
    if (last.session !== session || pendingId && last.pendingId !== pendingId) {
      unread.current = null; setHasUnread(false); setAnnouncement(''); nearBottom.current = true;
      showReply(null); setAway(false); return;
    }
    if (end <= last.end) return;
    const firstReply = [...(area.current?.querySelectorAll<HTMLElement>('.message.assistant[data-message-index]') ?? [])]
      .find(node => Number(node.dataset.messageIndex) >= last.end);
    const index = firstReply ? Number(firstReply.dataset.messageIndex) : null;
    if (index !== null) setAnnouncement('收到新回复');
    if (nearBottom.current) { showReply(index); onScroll(); }
    else if (index !== null) { unread.current ??= index; setHasUnread(true); }
  }, [session, end, pendingId]);
  return { away, hasUnread, announcement, onScroll, jumpToLatest: () => {
    showReply(unread.current, true); unread.current = null; setHasUnread(false);
  } };
}
