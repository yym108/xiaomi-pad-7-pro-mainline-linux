package com.muyu.ubuntutile;

import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.widget.Toast;

public class Switcher {
    public static void switchToUbuntu(final Context ctx) {
        new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    Process p = Runtime.getRuntime().exec(
                        new String[]{"su", "-c", "/data/adb/switch-slot b"}
                    );
                    int rc = p.waitFor();
                    if (rc != 0) {
                        showToast(ctx, "请先在 KernelSU Next 授予「切换 Arch Linux」Root 权限");
                    }
                } catch (Exception e) {
                    showToast(ctx, "切换失败: " + e.getMessage());
                }
            }
        }).start();
    }

    private static void showToast(final Context ctx, final String msg) {
        new Handler(Looper.getMainLooper()).post(new Runnable() {
            @Override
            public void run() {
                Toast.makeText(ctx, msg, Toast.LENGTH_LONG).show();
            }
        });
    }
}
