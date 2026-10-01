package dev.tvprobe.mediasession;

import android.app.Activity;
import android.content.ComponentName;
import android.os.Build;
import android.os.Bundle;
import android.service.notification.NotificationListenerService;

public class StartActivity extends Activity {
    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        if (Build.VERSION.SDK_INT >= 24) {
            NotificationListenerService.requestRebind(new ComponentName(this, ProbeService.class));
        }
        finish();
    }
}
