import { useMutation, useQueryClient } from "@tanstack/react-query";
import { OctagonPause, ShieldAlert } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/input";
import { api } from "@/lib/api";
import { keys, useSystemStatus } from "@/lib/queries";

function useKillSwitch() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: { engaged: boolean; reason?: string }) =>
      api.post("/api/system/kill-switch", body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: keys.status });
      void queryClient.invalidateQueries({ queryKey: keys.audit });
    },
  });
}

export function KillSwitchBanner() {
  const { data } = useSystemStatus();
  const mutation = useKillSwitch();
  if (!data?.kill_switch.engaged) return null;
  return (
    <div
      role="alert"
      className="flex flex-wrap items-center gap-3 border-b border-destructive/40 bg-destructive/10 px-4 py-2 text-sm md:px-8"
    >
      <ShieldAlert className="size-4 text-destructive" />
      <span className="flex-1">
        <strong>Kill switch is on.</strong> Jarvis won't carry out any action until you release it.
      </span>
      <Button
        size="sm"
        variant="outline"
        onClick={() => mutation.mutate({ engaged: false })}
        disabled={mutation.isPending}
      >
        Release
      </Button>
    </div>
  );
}

export function KillSwitchButton() {
  const { data } = useSystemStatus();
  const mutation = useKillSwitch();
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const engaged = data?.kill_switch.engaged ?? false;

  if (engaged) {
    return (
      <Button
        variant="outline"
        onClick={() => mutation.mutate({ engaged: false })}
        disabled={mutation.isPending}
      >
        Release kill switch
      </Button>
    );
  }
  return (
    <>
      <Button variant="destructive" onClick={() => setOpen(true)}>
        <OctagonPause /> Stand down
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogTitle>Stop all autonomy?</DialogTitle>
          <DialogDescription>
            Jarvis will stop executing every action immediately, including ones you already
            approved. Nothing is lost: they wait until you release the switch.
          </DialogDescription>
          <Textarea
            aria-label="Reason (optional)"
            placeholder="Reason (optional)"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={() =>
                mutation.mutate(
                  { engaged: true, reason: reason || undefined },
                  { onSuccess: () => setOpen(false) },
                )
              }
              disabled={mutation.isPending}
            >
              Engage kill switch
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
