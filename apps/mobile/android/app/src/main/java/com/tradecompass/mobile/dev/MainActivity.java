package com.tradecompass.mobile.dev;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override public void onCreate(android.os.Bundle savedInstanceState) {
        registerPlugin(CompassPlugin.class);
        super.onCreate(savedInstanceState);
    }
}
