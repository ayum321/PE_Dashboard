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

import { createContext } from 'react';
import { LightTheme } from '@jda/lui-common-component-library';
import { EventsContext } from '../components/events-handler/EventsHandler';

export const EventContext = createContext<EventsContext>({
  theme: LightTheme,
});
