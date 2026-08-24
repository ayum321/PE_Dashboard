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

import { MockPortalMessageService, PortalMessageService } from '@jda/lui-portal-utilities';
import { handleBreadcrumbs, handleThemeRequest } from './EventEmmiters';

describe('EventEmmiters', () => {
  beforeEach(() => {
    jest.spyOn(PortalMessageService, 'getInstance').mockReturnValue(new MockPortalMessageService());
  });

  afterEach(() => {
    jest.clearAllMocks();
  });

  it('should emmit the handleThemeRequest event', () => {
    handleThemeRequest();
    expect(PortalMessageService.getInstance).toHaveBeenCalled();
  });

  it('should emmit the handleBreadcrumbs event', () => {
    handleBreadcrumbs([]);
    expect(PortalMessageService.getInstance).toHaveBeenCalled();
  });
});
