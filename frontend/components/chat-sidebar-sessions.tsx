"use client";

import { useMemo } from "react";
import Link from "next/link";
import { Loader2, Search } from "lucide-react";

import { useChatSessions } from "@/components/chat-sessions-provider";
import {
  SidebarInput,
  SidebarMenuSub,
  SidebarMenuSubButton,
  SidebarMenuSubItem,
} from "@/components/ui/sidebar";
import { buildChatHref } from "@/lib/chat-session-routing";
import { buildHistorySections, filterThreadsByQuery } from "@/lib/chat-thread-model";

function ChatSessionTitle({ title }: { title: string }) {
  return (
    <span className="sidebar-chat-session-title">
      <span className="sidebar-chat-session-title-text">{title}</span>
    </span>
  );
}

export function ChatSidebarSessions() {
  const {
    threads,
    loading,
    historyQuery,
    setHistoryQuery,
    activeThreadId,
  } = useChatSessions();

  const historySections = useMemo(() => {
    return buildHistorySections(filterThreadsByQuery(threads, historyQuery));
  }, [historyQuery, threads]);

  return (
    <div className="sidebar-chat-sessions group-data-[collapsible=icon]:hidden">
      <SidebarMenuSub className="sidebar-chat-submenu mx-0 translate-x-0 border-0 px-0 py-1">
        <SidebarMenuSubItem className="relative flex items-center">
          <Search className="pointer-events-none absolute left-2.5 size-3.5 text-muted-foreground" />
          <SidebarInput
            className="sidebar-chat-search-input"
            onChange={(event) => setHistoryQuery(event.target.value)}
            placeholder="Search chats"
            value={historyQuery}
          />
        </SidebarMenuSubItem>

        {loading ? (
          <SidebarMenuSubItem>
            <div className="sidebar-chat-sessions-empty">
              <Loader2 className="size-3.5 animate-spin" />
              <span>Loading sessions...</span>
            </div>
          </SidebarMenuSubItem>
        ) : historySections.length === 0 ? (
          <SidebarMenuSubItem>
            <div className="sidebar-chat-sessions-empty">
              <span>{historyQuery.trim() ? "No matching sessions." : "No sessions yet."}</span>
            </div>
          </SidebarMenuSubItem>
        ) : (
          historySections.map((section) => (
            <li className="sidebar-chat-history-group" key={section.label}>
              <p className="sidebar-chat-history-label">{section.label}</p>
              <ul className="sidebar-chat-history-items">
                {section.items.map((thread) => (
                  <SidebarMenuSubItem key={thread.id}>
                    <SidebarMenuSubButton
                      className={thread.id === activeThreadId ? "sidebar-chat-session-link is-active" : "sidebar-chat-session-link"}
                      isActive={thread.id === activeThreadId}
                      render={<Link href={buildChatHref(thread.id)} />}
                      size="sm"
                    >
                      <ChatSessionTitle title={thread.title} />
                    </SidebarMenuSubButton>
                  </SidebarMenuSubItem>
                ))}
              </ul>
            </li>
          ))
        )}
      </SidebarMenuSub>
    </div>
  );
}
