interface MessagePage {
  session_id: string;
  messages: unknown[];
  page: { start_index: number; next_before: number | null };
}

// Join the newest page to an already displayed range before publishing either.
// Cursors are transcript positions, not message content: repeated text is valid.
export async function fillHistoryGap<P extends MessagePage>(
  latest: P,
  previousEnd: number | undefined,
  loadBefore: (before: number) => Promise<P>,
  signal: AbortSignal,
): Promise<P> {
  let result = latest;
  while (previousEnd !== undefined && result.page.start_index > previousEnd) {
    signal.throwIfAborted();
    const before = result.page.start_index;
    const older = await loadBefore(before);
    signal.throwIfAborted();
    if (older.session_id !== latest.session_id || older.page.start_index >= before
        || older.page.start_index + older.messages.length !== before) {
      throw new Error('会话历史暂未同步完整，请稍后重试');
    }
    result = { ...result, messages: [...older.messages, ...result.messages],
      page: { ...result.page, start_index: older.page.start_index, next_before: older.page.next_before } };
  }
  return result;
}
