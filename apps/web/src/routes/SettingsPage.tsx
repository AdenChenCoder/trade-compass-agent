import { PageHeading } from "@/components/layout/PageHeading";
import { Sparkles, Settings, Palette, Bot, Smartphone } from "lucide-react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { AgentExtensions } from "@/components/AgentExtensions";
import { MobileConnection } from "@/components/MobileConnection";
import { AgentConnection } from "@/components/AgentConnection";
import { AppearanceSettings } from "@/components/AppearanceSettings";

export function SettingsPage() {
  return (
    <div className="workspace-page settings-page">
      <div className="page-container">
        <PageHeading icon={Settings} title="设置" />

        <Tabs defaultValue={({ "#agent-connection": "agent", "#mobile-connection": "mobile", "#extensions": "extensions" } as Record<string, string>)[window.location.hash] ?? "appearance"} className="settings-tabs">
          <TabsList><TabsTrigger value="appearance"><Palette size={16} />外观</TabsTrigger><TabsTrigger value="agent"><Bot size={16} />Agent 接入</TabsTrigger><TabsTrigger value="mobile"><Smartphone size={16} />手机连接</TabsTrigger><TabsTrigger value="extensions"><Sparkles size={16} />技能与工具</TabsTrigger></TabsList>
          <TabsContent value="appearance" forceMount><section id="appearance" aria-label="外观设置"><AppearanceSettings /></section></TabsContent>
          <TabsContent value="agent" forceMount><section id="agent-connection" aria-label="Agent 接入设置"><AgentConnection /></section></TabsContent>
          <TabsContent value="mobile" forceMount><section id="mobile-connection" aria-label="手机连接设置"><MobileConnection /></section></TabsContent>
          <TabsContent value="extensions" forceMount><section id="extensions" aria-label="技能与工具" className="settings-extensions"><AgentExtensions /></section></TabsContent>
        </Tabs>
      </div>
    </div>
  );
}
