/*
 * ===============================================================================================================
 *                                Copyright 2021, Blue Yonder Group, Inc.
 *                                           All Rights Reserved
 *
 *                               THIS IS UNPUBLISHED PROPRIETARY SOURCE CODE OF
 *                                          BLUE YONDER GROUP, INC.
 *
 *
 *                         The copyright notice above does not evidence any actual
 *                                 or intended publication of such source code.
 *
 * ===============================================================================================================
 */

import React, { useEffect, useState } from 'react';
import { PortalMessageService, MessageActions, Message, ThemeSwitchResponseMessage } from '@jda/lui-portal-utilities';
import { DarkTheme, LightTheme } from '@jda/lui-common-component-library';
import { useMemoOne } from 'use-memo-one';
import { Theme } from '@material-ui/core';
import { EventContext } from '../../context';
import { handleThemeRequest } from './EventEmmiters';

interface Themes {
  dark: Theme;
  light: Theme;
}

const themes: Themes = {
  dark: DarkTheme,
  light: LightTheme,
};

export interface EventsContext {
  theme: Theme;
}

type EventsHandlerProps = {
  children: JSX.Element;
};

const getTheme = (themeName: string): Theme => (themeName === 'dark' ? themes.dark : themes.light);

export const EventsHandler = ({ children }: EventsHandlerProps) => {
  const [theme, setTheme] = useState<Theme>(LightTheme);

  const portalEventsHandler = (message: Message) => {
    switch (message.action) {
      case MessageActions.ThemeSwitchResponse:
        setTheme(getTheme((message as ThemeSwitchResponseMessage).theme));
        break;
    }
  };

  // Emmit values to portal
  useEffect(() => {
    PortalMessageService.getInstance().registerListener(portalEventsHandler);
    handleThemeRequest();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const providerValue = useMemoOne(() => ({ theme }), [theme]);

  return <EventContext.Provider value={providerValue}>{children}</EventContext.Provider>;
};
