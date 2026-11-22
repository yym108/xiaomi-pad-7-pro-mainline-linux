package com.muyu.ubuntutile;

import android.service.quicksettings.Tile;
import android.service.quicksettings.TileService;

public class UbuntuTileService extends TileService {
    @Override
    public void onStartListening() {
        super.onStartListening();
        Tile t = getQsTile();
        if (t != null) {
            t.setState(Tile.STATE_INACTIVE);
            t.setLabel("切换 Arch Linux");
            t.updateTile();
        }
    }

    @Override
    public void onClick() {
        super.onClick();
        Tile t = getQsTile();
        if (t != null) {
            t.setState(Tile.STATE_ACTIVE);
            t.setLabel("正在切换...");
            t.updateTile();
        }
        Switcher.switchToUbuntu(getApplicationContext());
    }
}
