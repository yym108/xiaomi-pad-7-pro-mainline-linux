package com.muyu.ubuntutile;

import android.app.Activity;
import android.os.Bundle;

public class SwitchActivity extends Activity {
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        Switcher.switchToUbuntu(getApplicationContext());
        finish();
    }
}
