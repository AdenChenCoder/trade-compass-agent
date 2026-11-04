import { useId, useState } from "react";
import { Check } from "lucide-react";
import { appearances, setAppearance, type Appearance } from "../../../shared/appearance";
import { useAppearance } from "@/lib/appearance";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";

export function AppearanceSettings() {
  const selected = useAppearance();
  const [saveFailed, setSaveFailed] = useState(false);
  const groupId = useId();
  const choose = (theme: Appearance) => setSaveFailed(!setAppearance(theme));

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">配色风格</CardTitle>
        <CardDescription id={`${groupId}-hint`}>自动保存在当前浏览器。</CardDescription>
      </CardHeader>
      <CardContent>
        <fieldset className="appearance-fieldset" aria-describedby={`${groupId}-hint`}>
          <legend className="sr-only">配色风格</legend>
          <div className="appearance-options">
            {appearances.map(theme => (
              <label key={theme.id} className="appearance-option">
                <input type="radio" name={groupId} value={theme.id} checked={selected === theme.id} onChange={() => choose(theme.id)} />
                <span className="appearance-option-body">
                  <span className="appearance-preview" data-theme={theme.id} aria-hidden="true">
                    <span className="appearance-preview-rail"><span className="brand-mark" /><i /><i /><i /></span>
                    <span className="appearance-preview-page"><span className="appearance-preview-heading" /><span className="appearance-preview-hero"><i /><i /></span><span className="appearance-preview-lines"><i /><i /></span></span>
                  </span>
                  <span className="appearance-option-label"><span><strong>{theme.name}</strong><small>{theme.palette}</small></span><Check size={16} aria-hidden="true" /></span>
                </span>
              </label>
            ))}
          </div>
        </fieldset>
        <div className="appearance-footer">
          <span role="status">当前：{appearances.find(theme => theme.id === selected)?.name ?? "默认配色"}</span>
          <Button variant="ghost" size="sm" disabled={selected === "classic"} onClick={() => choose("classic")}>恢复默认</Button>
        </div>
        {saveFailed ? <p className="mt-2 text-sm text-destructive" role="alert">配色已应用，但无法保存。下次打开时可能恢复默认配色，请检查浏览器的存储设置。</p> : null}
      </CardContent>
    </Card>
  );
}
