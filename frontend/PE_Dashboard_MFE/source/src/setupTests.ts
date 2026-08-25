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

import '@testing-library/jest-dom';
import 'jest-canvas-mock';

window.matchMedia =
  window.matchMedia ||
  function () {
    return {
      matches: false,
      addListener: function () {},
      removeListener: function () {},
    };
  };
