import { configureStore } from "@reduxjs/toolkit";

import { kondenApi } from "../api/kondenApi";
import runtimeReducer from "../realtime/runtimeSlice";

export const store = configureStore({
  reducer: {
    [kondenApi.reducerPath]: kondenApi.reducer,
    runtime: runtimeReducer,
  },
  middleware: (getDefaultMiddleware) =>
    getDefaultMiddleware().concat(kondenApi.middleware),
});

export type RootState = ReturnType<typeof store.getState>;
export type AppDispatch = typeof store.dispatch;
