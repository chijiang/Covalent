"use client";

import { useMemo } from "react";
import Link from "next/link";
import { Loader2, Plus } from "lucide-react";

import { useChatSessions } from "@/components/chat-sessions-provider";
import {
  SidebarInput,
  SidebarMenuSub,
  SidebarMenuSubButton,
  SidebarMenuSubItem,
} from "@/components/ui/sidebar";
import { buildChatHref } from "@/lib/chat-session-routing";
import { buildHistorySections, filterThreadsByQuery } from "@/lib/chat-thread-model";

export function ChatSidebarSessions() {
  const {
    threads,
    loading,
    historyQuery,
    setHistoryQuery,
    activeThreadId,
    handleNewChat,
  } = useChatSessions();

  const historySections = useMemo(() => {
    return buildHistorySections(filterThreadsByQuery(threads, historyQuery));
  }, [historyQuery, threads]);

  return (
    <div className="sidebar-chat-sessions group-data-[collapsible=icon]:hidden">
      <SidebarMenuSub className="sidebar-chat-submenu">
        <SidebarMenuSubItem>
          <SidebarInput
            className="sidebar-chat-search-input"
            onChange={(event) => setHistoryQuery(event.target.value)}
            placeholder="Search"
            value={historyQuery}
          />
        </SidebarMenuSubItem>

        <SidebarMenuSubItem>
          <SidebarMenuSubButton
            className="sidebar-chat-new-chat"
            render={<button onClick={() => handleNewChat()} type="button" />}
            size="sm"
          >
            <Plus className="size-3.5" />
            <span>New chat</span>
          </SidebarMenuSubButton>
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
                      <span>{thread.title}</span>
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
