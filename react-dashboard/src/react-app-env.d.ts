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

/// <reference types="react-scripts" />

interface RuntimeEnvironment {
	API_BASE_URL?: string;
	FRAME_URL_PATH?: string;
	LOCAL_APP_NAME?: string;
}

interface Window {
	env: RuntimeEnvironment;
}
