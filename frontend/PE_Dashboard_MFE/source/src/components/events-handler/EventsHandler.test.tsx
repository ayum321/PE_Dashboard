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

import React, { useContext } from 'react';
import { render } from '@testing-library/react';
import { act } from 'react-dom/test-utils';
import { PortalMessageService, MockPortalMessageService, ThemeSwitchResponseMessage } from '@jda/lui-portal-utilities';
import { EventsHandler } from './EventsHandler';
import { EventContext } from '../../context';

const ConsumerComponent = () => {
  const { theme } = useContext(EventContext);
  return (
    <div data-testid="consumer-component" className={theme.palette.text as unknown as string}>
      {JSON.stringify(theme.palette.text)}
    </div>
  );
};

const getRenderPrerequiteCallback = (theme: string) => {
  return async () => {
    let newMessage = new ThemeSwitchResponseMessage();
    newMessage.theme = theme;
    // @ts-expect-error
    PortalMessageService.getInstance().listener.call(null, newMessage);
  };
};

describe('EventsHandlers', () => {
  let windowSpy: jest.SpyInstance;

  beforeEach(() => {
    windowSpy = jest.spyOn(window, 'window', 'get');
    jest.spyOn(PortalMessageService, 'getInstance').mockReturnValue(new MockPortalMessageService());
  });

  afterEach(() => {
    jest.clearAllMocks();
    windowSpy.mockRestore();
  });

  it('should render', () => {
    const { getByTestId } = render(
      <EventsHandler>
        <ConsumerComponent />
      </EventsHandler>,
    );

    expect(getByTestId('consumer-component')).toBeDefined();
  });

  it('should update the theme on ThemeSwitchResponseMessage', async () => {
    const { getByTestId } = render(
      <EventsHandler>
        <ConsumerComponent />
      </EventsHandler>,
    );

    await act(getRenderPrerequiteCallback('dark'));

    expect(getByTestId('consumer-component')).toBeDefined();
  });

  it('should update the theme on ThemeSwitchResponseMessage to light', async () => {
    const { getByTestId } = render(
      <EventsHandler>
        <ConsumerComponent />
      </EventsHandler>,
    );

    await act(getRenderPrerequiteCallback('dark'));
    await act(getRenderPrerequiteCallback('light'));

    expect(getByTestId('consumer-component')).toBeDefined();
  });
});
