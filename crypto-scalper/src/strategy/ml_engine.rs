use tracing::{info, warn};

/// ML Decision Engine — Phase A (rule) → Phase B (Burn LSTM)
/// Phase A: 무조건 Go (데이터 수집)
/// Phase B: LSTM P(Win) ≥ threshold → Go

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum MlPhase {
    A, // Rule-based (< min_samples)
    B, // LSTM model active
}

pub struct MlEngine {
    pub phase: MlPhase,
    pub total_labeled: usize,
    min_samples: usize,
    retrain_interval: usize,
    go_threshold: f64,
    min_oos_accuracy: f64,
    last_train_count: usize,
    oos_accuracy: f64,
    // TODO: Burn LSTM model will go here
    // model: Option<ScalpLSTM<NdArray>>,
}

impl MlEngine {
    pub fn new(min_samples: usize, retrain_interval: usize,
               go_threshold: f64, min_oos_accuracy: f64) -> Self {
        Self {
            phase: MlPhase::A,
            total_labeled: 0,
            min_samples,
            retrain_interval,
            go_threshold,
            min_oos_accuracy,
            last_train_count: 0,
            oos_accuracy: 0.0,
        }
    }

    /// Go/NoGo 결정
    pub fn decide(&self, features: &[f64]) -> (bool, f64) {
        match self.phase {
            MlPhase::A => (true, -1.0), // 무조건 Go
            MlPhase::B => {
                // TODO: Burn LSTM inference
                // let prob = self.model.predict(features);
                // (prob > self.go_threshold, prob)
                (true, -1.0) // 임시: Phase B도 Go
            }
        }
    }

    /// 학습 체크 + 실행
    pub fn check_and_train(&mut self, labeled_count: usize, labeled_data: &[(Vec<f64>, i32, f64)]) {
        self.total_labeled = labeled_count;

        // Phase A → B 전환
        if self.total_labeled >= self.min_samples && self.phase == MlPhase::A {
            info!("[ML] {}건 도달 → Phase B 학습 시도", self.total_labeled);
            self.train(labeled_data);
            return;
        }

        // Phase B 재학습
        if self.phase == MlPhase::B &&
           (self.total_labeled - self.last_train_count) >= self.retrain_interval {
            info!("[ML] 재학습: {}건 (이전 {})", self.total_labeled, self.last_train_count);
            self.train(labeled_data);
        }
    }

    fn train(&mut self, data: &[(Vec<f64>, i32, f64)]) {
        if data.len() < self.min_samples {
            return;
        }

        // TODO: Burn LSTM 학습 구현
        // Walk-Forward 80/20 split
        // let split = (data.len() as f64 * 0.8) as usize;
        // let (train, test) = data.split_at(split);
        //
        // let model = ScalpLSTM::new(&device);
        // ... train loop ...
        // let oos_acc = evaluate(&model, test);
        //
        // if oos_acc >= self.min_oos_accuracy {
        //     self.model = Some(model);
        //     self.phase = MlPhase::B;
        //     self.oos_accuracy = oos_acc;
        //     self.last_train_count = self.total_labeled;
        // }

        info!("[ML] 학습 완료 (placeholder — Burn LSTM 구현 예정)");
        self.last_train_count = self.total_labeled;
    }
}
