// Wrap an async UI action so a failed API call shows its message instead of failing silently.
export const guarded = (action) => async (...args) => {
  try {
    return await action(...args);
  } catch (e) {
    window.alert(e.message);
  }
};
